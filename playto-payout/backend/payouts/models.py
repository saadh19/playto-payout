"""
Playto Payout Engine - Core Models

Design principles:
- All amounts stored as BigIntegerField in paise (1 INR = 100 paise)
- Balance is DERIVED from ledger entries, never stored directly (avoids double-write bugs)
- State machine enforced at model level via explicit transition methods
- No FloatField or DecimalField for money anywhere
"""
import uuid
from django.db import models, transaction
from django.db.models import Sum, Q
from django.utils import timezone


class Merchant(models.Model):
    """
    Represents an Indian agency/freelancer using Playto Pay.
    Balance is computed from LedgerEntry rows — never stored as a field.
    This ensures the invariant: SUM(credits) - SUM(debits) == displayed balance.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def get_balance_summary(self):
        """
        Computes balance at the DB level using a single aggregation query.
        We never pull rows into Python and sum there — that's a race condition.

        Returns dict with:
        - available_paise: spendable balance
        - held_paise: funds locked in pending/processing payouts
        - total_credited_paise: lifetime credits
        """
        agg = self.ledger_entries.aggregate(
            total_credits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.CREDIT)),
            total_debits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.DEBIT)),
        )
        total_credits = agg['total_credits'] or 0
        total_debits = agg['total_debits'] or 0

        # Held funds = debit entries linked to pending or processing payouts
        held = self.ledger_entries.filter(
            entry_type=LedgerEntry.DEBIT,
            payout__status__in=[Payout.PENDING, Payout.PROCESSING],
        ).aggregate(total=Sum('amount_paise'))['total'] or 0

        return {
            'available_paise': total_credits - total_debits,
            'held_paise': held,
            'total_credited_paise': total_credits,
        }

    def __str__(self):
        return f"{self.name} ({self.email})"

    class Meta:
        db_table = 'merchants'


class BankAccount(models.Model):
    """Saved bank account details for a merchant."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name='bank_accounts')
    account_number = models.CharField(max_length=20)
    ifsc_code = models.CharField(max_length=11)
    account_holder_name = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.account_holder_name} - {self.account_number[-4:].rjust(len(self.account_number), '*')}"

    class Meta:
        db_table = 'bank_accounts'


class Payout(models.Model):
    """
    A merchant's request to withdraw funds to their bank account.

    State machine:
        PENDING → PROCESSING → COMPLETED
        PENDING → PROCESSING → FAILED
        FAILED → PENDING (retry only, via retry_attempt field)

    Illegal transitions are blocked in transition methods below.
    Funds are held (debited from ledger) when payout is created.
    On FAILED, funds are returned atomically with the state change.
    """
    PENDING = 'pending'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'

    STATUS_CHOICES = [
        (PENDING, 'Pending'),
        (PROCESSING, 'Processing'),
        (COMPLETED, 'Completed'),
        (FAILED, 'Failed'),
    ]

    # Valid forward transitions
    VALID_TRANSITIONS = {
        PENDING: {PROCESSING},
        PROCESSING: {COMPLETED, FAILED},
        COMPLETED: set(),   # terminal state
        FAILED: set(),      # terminal state (retry creates new payout)
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='payouts')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='payouts')
    amount_paise = models.BigIntegerField()  # NEVER float, NEVER decimal
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING, db_index=True)

    # Retry tracking
    retry_attempt = models.PositiveSmallIntegerField(default=0)
    max_retries = models.PositiveSmallIntegerField(default=3)

    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)

    # Error tracking
    failure_reason = models.TextField(blank=True, default='')

    # Idempotency link (set when this payout was created from an idempotency key)
    idempotency_record = models.OneToOneField(
        'IdempotencyKey',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='payout',
    )

    def transition_to(self, new_status, failure_reason=''):
        """
        Enforce state machine. Raises ValueError on illegal transitions.
        This is the single chokepoint for all status changes.
        Call this inside a transaction when changing status.
        """
        allowed = self.VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal payout transition: {self.status} → {new_status} "
                f"(payout {self.id})"
            )

        if new_status == self.PROCESSING:
            self.processing_started_at = timezone.now()

        if new_status == self.FAILED:
            self.failure_reason = failure_reason

        self.status = new_status
        self.save(update_fields=['status', 'failure_reason', 'processing_started_at', 'updated_at'])

    def __str__(self):
        return f"Payout {self.id} — ₹{self.amount_paise / 100:.2f} [{self.status}]"

    class Meta:
        db_table = 'payouts'
        indexes = [
            models.Index(fields=['merchant', 'status']),
            models.Index(fields=['status', 'processing_started_at']),
        ]


class LedgerEntry(models.Model):
    """
    Immutable append-only record of every money movement.

    CREDIT: funds added to merchant (customer payment, refund of failed payout)
    DEBIT: funds removed from merchant (payout hold)

    Balance = SUM(credits) - SUM(debits) — computed at query time, never cached.
    Entries are NEVER updated or deleted.
    """
    CREDIT = 'credit'
    DEBIT = 'debit'
    ENTRY_TYPE_CHOICES = [
        (CREDIT, 'Credit'),
        (DEBIT, 'Debit'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='ledger_entries')
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES, db_index=True)
    amount_paise = models.BigIntegerField()  # always positive

    # Link to payout if this entry is a hold or refund
    payout = models.ForeignKey(
        Payout,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='ledger_entries',
    )

    description = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'ledger_entries'
        ordering = ['-created_at']

    def __str__(self):
        sign = '+' if self.entry_type == self.CREDIT else '-'
        return f"{sign}₹{self.amount_paise / 100:.2f} — {self.description}"


class IdempotencyKey(models.Model):
    """
    Records every processed idempotency key to prevent duplicate payout creation.

    Scope: (merchant_id, key) pair — keys are merchant-scoped.
    If a second request arrives with the same key:
      - If response_body is set: return the cached response immediately.
      - If response_body is null (first request still in-flight): return 409.

    Keys expire after 24 hours (checked at lookup time).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name='idempotency_keys')
    key = models.CharField(max_length=255, db_index=True)

    # Cached response — null means first request is still in flight
    response_body = models.JSONField(null=True, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'idempotency_keys'
        unique_together = [('merchant', 'key')]
        indexes = [
            models.Index(fields=['merchant', 'key']),
        ]

    def is_expired(self):
        from django.conf import settings
        ttl = getattr(settings, 'IDEMPOTENCY_KEY_TTL', 86400)
        return (timezone.now() - self.created_at).total_seconds() > ttl

    def __str__(self):
        return f"IdempKey {self.key[:8]}… for {self.merchant_id}"
