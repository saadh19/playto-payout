"""
Playto Payout Engine - API Views

Critical correctness properties implemented here:
1. Concurrency: SELECT FOR UPDATE with NOWAIT on merchant ledger to prevent overdraw
2. Idempotency: get_or_create on IdempotencyKey, 409 if in-flight
3. Balance check + debit atomically inside a DB transaction
"""
import logging
from django.db import transaction, IntegrityError
from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Merchant, BankAccount, Payout, LedgerEntry, IdempotencyKey
from .serializers import (
    MerchantDashboardSerializer,
    PayoutSerializer,
    LedgerEntrySerializer,
    CreatePayoutSerializer,
    MerchantSerializer,
)
from .tasks import process_payout

logger = logging.getLogger(__name__)


@api_view(['GET'])
def list_merchants(request):
    """List all merchants — used by frontend to populate dropdown."""
    merchants = Merchant.objects.all().order_by('name')
    return Response(MerchantSerializer(merchants, many=True).data)


@api_view(['GET'])
def merchant_dashboard(request, merchant_id):
    """
    Returns merchant balance summary and bank accounts.
    Balance is computed at DB level — see Merchant.get_balance_summary().
    """
    try:
        merchant = Merchant.objects.prefetch_related('bank_accounts').get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found.'}, status=status.HTTP_404_NOT_FOUND)

    return Response(MerchantDashboardSerializer(merchant).data)


@api_view(['GET'])
def merchant_ledger(request, merchant_id):
    """Paginated ledger entries for a merchant."""
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found.'}, status=status.HTTP_404_NOT_FOUND)

    entries = LedgerEntry.objects.filter(merchant=merchant).select_related('payout').order_by('-created_at')[:50]
    return Response(LedgerEntrySerializer(entries, many=True).data)


@api_view(['GET'])
def merchant_payouts(request, merchant_id):
    """All payouts for a merchant, newest first."""
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found.'}, status=status.HTTP_404_NOT_FOUND)

    payouts = Payout.objects.filter(merchant=merchant).select_related('bank_account').order_by('-created_at')[:50]
    return Response(PayoutSerializer(payouts, many=True).data)


@api_view(['POST'])
def create_payout(request, merchant_id):
    """
    POST /api/v1/merchants/{merchant_id}/payouts
    Header: Idempotency-Key: <uuid>

    Creates a payout request. The critical section:
    1. Validate input
    2. Handle idempotency (return cached response if key seen before)
    3. LOCK merchant ledger rows with SELECT FOR UPDATE NOWAIT
    4. Compute available balance at DB level
    5. Check sufficient funds
    6. Create payout + debit ledger entry atomically
    7. Cache idempotency response
    8. Dispatch background worker
    """
    idempotency_key = request.headers.get('Idempotency-Key')
    if not idempotency_key:
        return Response(
            {'error': 'Idempotency-Key header is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found.'}, status=status.HTTP_404_NOT_FOUND)

    # --- Idempotency check ---
    # Use get_or_create so two concurrent requests racing on the same key
    # will have exactly one win the INSERT. The loser gets the existing row.
    idem_key_obj, created = IdempotencyKey.objects.get_or_create(
        merchant=merchant,
        key=idempotency_key,
    )

    if not created:
        # Key was seen before — check expiry
        if idem_key_obj.is_expired():
            # Expired key: delete it and treat as new
            idem_key_obj.delete()
            idem_key_obj, created = IdempotencyKey.objects.get_or_create(
                merchant=merchant,
                key=idempotency_key,
            )
        elif idem_key_obj.response_body is not None:
            # First request completed — return identical response
            logger.info(f"Idempotency hit for key {idempotency_key[:8]}…")
            return Response(idem_key_obj.response_body, status=idem_key_obj.response_status)
        else:
            # First request is still in flight (response_body is None)
            return Response(
                {'error': 'A request with this idempotency key is already in progress.'},
                status=status.HTTP_409_CONFLICT,
            )

    # --- Validate input ---
    serializer = CreatePayoutSerializer(data=request.data)
    if not serializer.is_valid():
        # Clean up the idempotency record since we're rejecting the request
        idem_key_obj.delete()
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    amount_paise = serializer.validated_data['amount_paise']
    bank_account_id = serializer.validated_data['bank_account_id']

    try:
        bank_account = BankAccount.objects.get(id=bank_account_id, merchant=merchant)
    except BankAccount.DoesNotExist:
        idem_key_obj.delete()
        return Response({'error': 'Bank account not found for this merchant.'}, status=status.HTTP_404_NOT_FOUND)

    # --- Critical section: balance check + debit atomically ---
    try:
        with transaction.atomic():
            # SELECT FOR UPDATE NOWAIT on all ledger entries for this merchant.
            # This acquires row-level locks. NOWAIT means if another transaction
            # already holds the lock, we get an error immediately (no blocking wait).
            # This prevents two concurrent payout requests from both reading the
            # same balance and both deciding they have enough funds.
            #
            # The lock is on ledger_entries rows. Any concurrent payout attempt
            # that tries to SELECT FOR UPDATE will fail with OperationalError,
            # which we catch and return 429.
            locked_entries = list(
                LedgerEntry.objects.select_for_update(nowait=True)
                .filter(merchant=merchant)
                .values('entry_type', 'amount_paise', 'payout_id')
            )

            # Compute balance from locked snapshot — purely in Python here
            # is safe because we hold the locks (no concurrent writer can modify).
            credits = sum(e['amount_paise'] for e in locked_entries if e['entry_type'] == LedgerEntry.CREDIT)
            debits = sum(e['amount_paise'] for e in locked_entries if e['entry_type'] == LedgerEntry.DEBIT)
            available_paise = credits - debits

            if available_paise < amount_paise:
                raise InsufficientFundsError(
                    f"Insufficient funds. Available: {available_paise} paise, requested: {amount_paise} paise."
                )

            # Create payout
            payout = Payout.objects.create(
                merchant=merchant,
                bank_account=bank_account,
                amount_paise=amount_paise,
                status=Payout.PENDING,
                idempotency_record=idem_key_obj,
            )

            # Debit ledger — this is the "hold"
            LedgerEntry.objects.create(
                merchant=merchant,
                entry_type=LedgerEntry.DEBIT,
                amount_paise=amount_paise,
                payout=payout,
                description=f'Hold for payout {payout.id}',
            )

        # Transaction committed — cache the response
        response_data = PayoutSerializer(payout).data
        # Convert UUIDs/datetimes to JSON-safe types for caching
        import json
        from rest_framework.renderers import JSONRenderer
        json_str = JSONRenderer().render(response_data)
        import json as _json
        cacheable = _json.loads(json_str)

        idem_key_obj.response_body = cacheable
        idem_key_obj.response_status = 201
        idem_key_obj.save(update_fields=['response_body', 'response_status'])

        # Dispatch background processor
        process_payout.apply_async(args=[str(payout.id)], countdown=1)

        return Response(cacheable, status=status.HTTP_201_CREATED)

    except InsufficientFundsError as e:
        idem_key_obj.delete()
        return Response({'error': str(e)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    except Exception as e:
        # Covers OperationalError from NOWAIT lock contention
        error_str = str(e).lower()
        if 'could not obtain lock' in error_str or 'lock' in error_str:
            idem_key_obj.delete()
            return Response(
                {'error': 'Another payout is being processed. Please try again shortly.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        logger.exception(f"Unexpected error creating payout for merchant {merchant_id}")
        idem_key_obj.delete()
        return Response({'error': 'Internal server error.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
def payout_detail(request, merchant_id, payout_id):
    """Get a single payout — used for polling status."""
    try:
        payout = Payout.objects.select_related('bank_account').get(
            id=payout_id, merchant_id=merchant_id
        )
    except Payout.DoesNotExist:
        return Response({'error': 'Payout not found.'}, status=status.HTTP_404_NOT_FOUND)

    return Response(PayoutSerializer(payout).data)


class InsufficientFundsError(Exception):
    pass
