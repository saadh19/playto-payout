"""
Tests for the two most critical correctness properties:
1. Concurrency: two simultaneous 60-rupee payout requests on a 100-rupee balance
2. Idempotency: same key returns same response, no duplicate payout created
"""
import uuid
import threading
import time
from django.test import TestCase, TransactionTestCase
from django.test.client import Client
from django.urls import reverse
from rest_framework.test import APIClient

from payouts.models import Merchant, BankAccount, LedgerEntry, Payout, IdempotencyKey


def make_merchant(name='Test Merchant', email=None, balance_paise=10000):
    """Helper: create a merchant with a given balance via ledger credit."""
    if email is None:
        email = f"{uuid.uuid4().hex[:8]}@test.com"
    merchant = Merchant.objects.create(name=name, email=email)
    bank = BankAccount.objects.create(
        merchant=merchant,
        account_number='00123456789',
        ifsc_code='HDFC0001234',
        account_holder_name=name,
        is_primary=True,
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        entry_type=LedgerEntry.CREDIT,
        amount_paise=balance_paise,
        description='Initial credit for test',
    )
    return merchant, bank


class ConcurrencyTest(TransactionTestCase):
    """
    TransactionTestCase is required (not TestCase) because we need actual DB
    transactions to commit for concurrent threads to see each other's writes.
    TestCase wraps everything in a single transaction that never commits.
    """

    def test_concurrent_payouts_only_one_succeeds(self):
        """
        Merchant has 10,000 paise (₹100).
        Two threads simultaneously request 6,000 paise (₹60) payouts.
        Exactly one should succeed (201), the other should fail (422 or 429).
        """
        merchant, bank = make_merchant(balance_paise=10000)
        client = APIClient()

        results = []
        errors = []

        def attempt_payout(idem_key):
            try:
                response = client.post(
                    f'/api/v1/merchants/{merchant.id}/payouts/create/',
                    {'amount_paise': 6000, 'bank_account_id': str(bank.id)},
                    HTTP_IDEMPOTENCY_KEY=idem_key,
                    format='json',
                )
                results.append(response.status_code)
            except Exception as e:
                errors.append(str(e))

        # Two concurrent threads, different idempotency keys
        t1 = threading.Thread(target=attempt_payout, args=(str(uuid.uuid4()),))
        t2 = threading.Thread(target=attempt_payout, args=(str(uuid.uuid4()),))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(results), 2, "Expected exactly 2 responses")

        successes = results.count(201)
        failures = [r for r in results if r in (422, 429)]

        self.assertEqual(successes, 1, f"Expected exactly 1 success, got results: {results}")
        self.assertEqual(len(failures), 1, f"Expected exactly 1 failure, got results: {results}")

        # Verify ledger integrity: balance should be 10000 - 6000 = 4000 (held)
        summary = merchant.get_balance_summary()
        self.assertEqual(
            summary['available_paise'], 4000,
            f"Balance integrity violated. Summary: {summary}"
        )

    def test_balance_invariant(self):
        """
        credits - debits == available_paise must always hold.
        """
        from django.db.models import Sum
        merchant, bank = make_merchant(balance_paise=50000)
        summary = merchant.get_balance_summary()

        credits = LedgerEntry.objects.filter(
            merchant=merchant, entry_type=LedgerEntry.CREDIT
        ).aggregate(total=Sum('amount_paise'))['total'] or 0

        debits = LedgerEntry.objects.filter(
            merchant=merchant, entry_type=LedgerEntry.DEBIT
        ).aggregate(total=Sum('amount_paise'))['total'] or 0

        self.assertEqual(credits - debits, summary['available_paise'])


class IdempotencyTest(TestCase):

    def test_same_key_returns_same_response(self):
        """
        Two POST requests with the same idempotency key must return identical responses.
        No duplicate payout should be created.
        """
        merchant, bank = make_merchant(balance_paise=100000)
        client = APIClient()
        idem_key = str(uuid.uuid4())

        # First request
        r1 = client.post(
            f'/api/v1/merchants/{merchant.id}/payouts/create/',
            {'amount_paise': 5000, 'bank_account_id': str(bank.id)},
            HTTP_IDEMPOTENCY_KEY=idem_key,
            format='json',
        )
        self.assertEqual(r1.status_code, 201)

        # Second request — same key
        r2 = client.post(
            f'/api/v1/merchants/{merchant.id}/payouts/create/',
            {'amount_paise': 5000, 'bank_account_id': str(bank.id)},
            HTTP_IDEMPOTENCY_KEY=idem_key,
            format='json',
        )
        self.assertEqual(r2.status_code, 201)

        # Responses must be identical
        self.assertEqual(r1.data['id'], r2.data['id'], "Two different payouts were created!")

        # Only one payout in DB
        payout_count = Payout.objects.filter(merchant=merchant).count()
        self.assertEqual(payout_count, 1, f"Expected 1 payout, found {payout_count}")

    def test_different_merchants_same_key_creates_separate_payouts(self):
        """
        Idempotency keys are scoped per merchant.
        Same key for two different merchants should create two separate payouts.
        """
        m1, b1 = make_merchant(email='m1@test.com', balance_paise=100000)
        m2, b2 = make_merchant(email='m2@test.com', balance_paise=100000)
        client = APIClient()
        shared_key = str(uuid.uuid4())

        r1 = client.post(
            f'/api/v1/merchants/{m1.id}/payouts/create/',
            {'amount_paise': 5000, 'bank_account_id': str(b1.id)},
            HTTP_IDEMPOTENCY_KEY=shared_key,
            format='json',
        )
        r2 = client.post(
            f'/api/v1/merchants/{m2.id}/payouts/create/',
            {'amount_paise': 5000, 'bank_account_id': str(b2.id)},
            HTTP_IDEMPOTENCY_KEY=shared_key,
            format='json',
        )

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r1.data['id'], r2.data['id'])

    def test_missing_idempotency_key_rejected(self):
        """Requests without Idempotency-Key header must be rejected with 400."""
        merchant, bank = make_merchant(balance_paise=100000)
        client = APIClient()

        r = client.post(
            f'/api/v1/merchants/{merchant.id}/payouts/create/',
            {'amount_paise': 5000, 'bank_account_id': str(bank.id)},
            format='json',
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('Idempotency-Key', r.data['error'])

    def test_insufficient_funds_rejected(self):
        """Payout for more than available balance returns 422."""
        merchant, bank = make_merchant(balance_paise=1000)  # only ₹10
        client = APIClient()

        r = client.post(
            f'/api/v1/merchants/{merchant.id}/payouts/create/',
            {'amount_paise': 5000, 'bank_account_id': str(bank.id)},  # ₹50 requested
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            format='json',
        )
        self.assertEqual(r.status_code, 422)


class StateMachineTest(TestCase):

    def test_illegal_transition_raises(self):
        """Completed and failed are terminal states — no backwards transitions."""
        merchant, bank = make_merchant(balance_paise=100000)
        payout = Payout.objects.create(
            merchant=merchant,
            bank_account=bank,
            amount_paise=5000,
            status=Payout.COMPLETED,
        )

        with self.assertRaises(ValueError):
            payout.transition_to(Payout.PENDING)

        with self.assertRaises(ValueError):
            payout.transition_to(Payout.FAILED)

    def test_failed_payout_blocks_completed_transition(self):
        merchant, bank = make_merchant(balance_paise=100000)
        payout = Payout.objects.create(
            merchant=merchant,
            bank_account=bank,
            amount_paise=5000,
            status=Payout.FAILED,
        )
        with self.assertRaises(ValueError):
            payout.transition_to(Payout.COMPLETED)
