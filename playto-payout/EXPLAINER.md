# EXPLAINER.md — Playto Payout Engine

## 1. The Ledger

**Paste your balance calculation query. Why did you model credits and debits this way?**

Balance is computed on-demand from `LedgerEntry` rows. There is no `balance` column on the `Merchant` model.

```python
# payouts/models.py — Merchant.get_balance_summary()
agg = self.ledger_entries.aggregate(
    total_credits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.CREDIT)),
    total_debits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.DEBIT)),
)
total_credits = agg['total_credits'] or 0
total_debits = agg['total_debits'] or 0

held = self.ledger_entries.filter(
    entry_type=LedgerEntry.DEBIT,
    payout__status__in=[Payout.PENDING, Payout.PROCESSING],
).aggregate(total=Sum('amount_paise'))['total'] or 0
```

**Why this model?**

Storing balance as a denormalized column creates two failure modes:
1. **Double-write bug**: update the balance column AND write the ledger entry in separate statements — a crash between them leaves the system inconsistent.
2. **Concurrency drift**: two threads reading balance, both incrementing/decrementing, writing back — classic lost update.

The append-only ledger is the single source of truth. Balance is a mathematical consequence of the ledger, not something we maintain separately. The invariant `SUM(credits) - SUM(debits) == available_balance` holds by construction because balance *is* that sum.

`amount_paise` is `BigIntegerField` throughout. No floats, no decimals. 1 INR = 100 paise. Integer arithmetic is exact; float arithmetic is not.

---

## 2. The Lock

**Paste the exact code that prevents two concurrent payouts from overdrawing a balance. Explain what database primitive it relies on.**

```python
# payouts/views.py — create_payout()
with transaction.atomic():
    locked_entries = list(
        LedgerEntry.objects.select_for_update(nowait=True)
        .filter(merchant=merchant)
        .values('entry_type', 'amount_paise', 'payout_id')
    )

    credits = sum(e['amount_paise'] for e in locked_entries if e['entry_type'] == LedgerEntry.CREDIT)
    debits  = sum(e['amount_paise'] for e in locked_entries if e['entry_type'] == LedgerEntry.DEBIT)
    available_paise = credits - debits

    if available_paise < amount_paise:
        raise InsufficientFundsError(...)

    payout = Payout.objects.create(...)
    LedgerEntry.objects.create(entry_type=LedgerEntry.DEBIT, ...)
```

**Database primitive: `SELECT ... FOR UPDATE NOWAIT`**

This issues a PostgreSQL row-level exclusive lock on all ledger entry rows for the merchant. Key properties:

- **Exclusive**: no other transaction can acquire the same lock.
- **NOWAIT**: instead of blocking (which would serialize all requests anyway, causing a queue), we fail immediately if the lock is taken. The caller gets a 429 and can retry.
- **Atomic check-then-deduct**: the balance check and the debit INSERT happen inside the same transaction. No other writer can insert new ledger rows for this merchant until our transaction commits or rolls back.

Without this lock, two concurrent requests would both `SELECT` the same balance (e.g. 100 rupees), both pass the `100 >= 60` check, and both insert a debit — overdrawing by 20 rupees.

Why lock `LedgerEntry` rows rather than the `Merchant` row? Locking the merchant row would work, but it's a single hot row and would serialize all operations for a merchant. Locking ledger entries is more precise — it only blocks concurrent payout creation, not reads.

---

## 3. The Idempotency

**How does your system know it has seen a key before? What happens if the first request is in flight when the second arrives?**

```python
# payouts/views.py — create_payout()
idem_key_obj, created = IdempotencyKey.objects.get_or_create(
    merchant=merchant,
    key=idempotency_key,
)

if not created:
    if idem_key_obj.is_expired():
        idem_key_obj.delete()
        # treat as new
    elif idem_key_obj.response_body is not None:
        # First request completed — return cached response
        return Response(idem_key_obj.response_body, status=idem_key_obj.response_status)
    else:
        # First request still in flight (response_body is None)
        return Response({'error': '...'}, status=409)
```

**How it works:**

`IdempotencyKey` has a `UNIQUE(merchant_id, key)` constraint. `get_or_create` maps directly to a PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` — the database guarantees exactly one winner even under concurrent pressure. Two threads racing on the same key: one gets `created=True`, the other gets `created=False` with the existing row.

**`response_body` sentinel pattern:**

When a key is first created, `response_body` is `NULL`. This means: "a request is in flight." After the payout is successfully created and committed, we write the serialized response JSON into `response_body`. The second identical request:
- Sees `response_body IS NOT NULL` → returns the cached response (identical 201)
- Sees `response_body IS NULL` → returns 409 (first request still processing)

This handles the race between two requests at different stages of processing cleanly.

Keys are scoped to `(merchant_id, key)` — the same UUID from different merchants creates separate rows. Keys expire after 24 hours (checked in `IdempotencyKey.is_expired()`).

---

## 4. The State Machine

**Where in the code is failed-to-completed blocked? Show the check.**

```python
# payouts/models.py — Payout.transition_to()
VALID_TRANSITIONS = {
    Payout.PENDING:     {Payout.PROCESSING},
    Payout.PROCESSING:  {Payout.COMPLETED, Payout.FAILED},
    Payout.COMPLETED:   set(),   # terminal — no exits
    Payout.FAILED:      set(),   # terminal — no exits
}

def transition_to(self, new_status, failure_reason=''):
    allowed = self.VALID_TRANSITIONS.get(self.status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Illegal payout transition: {self.status} → {new_status} "
            f"(payout {self.id})"
        )
    # ... proceed
```

`COMPLETED` and `FAILED` both map to `set()` — no valid exits. Any attempt to call `payout.transition_to(Payout.COMPLETED)` on a `FAILED` payout raises `ValueError` before touching the database.

Every status mutation in the codebase goes through `transition_to()`. There is no direct assignment `payout.status = 'completed'` anywhere except inside this method. This makes the state machine impossible to bypass accidentally.

The fund-return credit is created inside the same `atomic()` block as `transition_to(FAILED)` in `tasks._fail_payout()`:

```python
with transaction.atomic():
    payout.transition_to(Payout.FAILED, failure_reason=reason)
    LedgerEntry.objects.create(
        entry_type=LedgerEntry.CREDIT,
        amount_paise=payout.amount_paise,
        ...
    )
```

If the credit INSERT fails, the status change rolls back. The merchant never loses money due to a partial failure.

---

## 5. The AI Audit

**One specific example where AI wrote subtly wrong code. Paste what it gave you, what you caught, and what you replaced it with.**

**The bug: Python-level aggregation without a lock**

When I asked the AI to implement the balance check for concurrent payout prevention, it generated:

```python
# What the AI gave me — WRONG
with transaction.atomic():
    summary = merchant.get_balance_summary()  # runs SELECT SUM(...)
    available = summary['available_paise']
    
    if available < amount_paise:
        raise InsufficientFundsError(...)
    
    payout = Payout.objects.create(...)
    LedgerEntry.objects.create(entry_type='debit', ...)
```

**Why this is wrong:**

`get_balance_summary()` runs a `SELECT SUM(...)` — a plain read with no lock. Two concurrent transactions can both run this SELECT simultaneously, both see `available = 10000`, both decide `10000 >= 6000`, and both proceed to INSERT their debit. PostgreSQL `REPEATABLE READ` doesn't help here because new rows (INSERTs by a concurrent transaction) are still visible after `BEGIN`. The check-then-act gap is the race condition.

Being inside `transaction.atomic()` does NOT prevent this. `atomic()` only guarantees atomicity of your own writes — it doesn't prevent other transactions from reading/writing concurrently unless you explicitly acquire locks.

**What I replaced it with:**

```python
# What I shipped — CORRECT
with transaction.atomic():
    # Acquire exclusive locks on all ledger rows for this merchant
    locked_entries = list(
        LedgerEntry.objects.select_for_update(nowait=True)
        .filter(merchant=merchant)
        .values('entry_type', 'amount_paise', 'payout_id')
    )
    
    credits = sum(e['amount_paise'] for e in locked_entries if e['entry_type'] == 'credit')
    debits  = sum(e['amount_paise'] for e in locked_entries if e['entry_type'] == 'debit')
    available = credits - debits

    if available < amount_paise:
        raise InsufficientFundsError(...)

    payout = Payout.objects.create(...)
    LedgerEntry.objects.create(entry_type='debit', ...)
```

`SELECT FOR UPDATE NOWAIT` acquires row-level exclusive locks before the check. Any concurrent transaction attempting the same lock fails immediately with an `OperationalError`, which we catch and return as 429. The lock is held until the transaction commits — making the check-then-deduct sequence atomic from PostgreSQL's perspective.

The sum in Python is now safe because we hold the locks: no concurrent INSERT can add a new ledger row until we commit.

**Second subtle issue the AI got wrong: idempotency check order**

The AI initially wrote:

```python
# AI's version — subtle race
try:
    existing = IdempotencyKey.objects.get(merchant=merchant, key=key)
    return Response(existing.response_body, ...)  # return cached
except IdempotencyKey.DoesNotExist:
    # first time seeing this key
    idem = IdempotencyKey.objects.create(merchant=merchant, key=key)
```

The `get` → `create` sequence is a TOCTOU race. Two concurrent requests with the same key both hit `DoesNotExist` and both attempt `create`. One succeeds, the other gets `IntegrityError` (from the UNIQUE constraint) — which would surface as a 500 if not caught, and either way doesn't return a clean 409.

I replaced it with `get_or_create()` which compiles to a single `INSERT ... ON CONFLICT` at the database level — exactly one thread wins the INSERT, the other gets the existing row atomically.
