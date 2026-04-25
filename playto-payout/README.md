# Playto Payout Engine

A minimal but production-correct payout engine for Playto Pay. Merchants accumulate balance via credits (simulated customer payments) and withdraw to their Indian bank accounts. Built with correctness-first principles: money integrity, concurrency safety, idempotency, and a strict state machine.

## Stack

- **Backend**: Django 4.2 + DRF
- **Database**: PostgreSQL (BigIntegerField for all money — no floats)
- **Background Jobs**: Celery + Redis
- **Frontend**: React + Vite + Tailwind CSS

## Quick Start (Docker)

```bash
git clone <repo>
cd playto-payout
docker-compose up --build
```

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000/api/v1/
- Django Admin: http://localhost:8000/admin/

## Manual Setup

### Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure .env
cp .env.example .env
# Edit DB_NAME, DB_USER, DB_PASSWORD, REDIS_URL

python manage.py migrate
python manage.py seed_merchants

# Terminal 1 — Django
python manage.py runserver

# Terminal 2 — Celery worker
celery -A config worker --loglevel=info

# Terminal 3 — Celery beat (retry stuck payouts every 30s)
celery -A config beat --loglevel=info
```

### Frontend

```bash
cd frontend
npm install
VITE_API_URL=http://localhost:8000/api/v1 npm run dev
```

## API

### List merchants
```
GET /api/v1/merchants/
```

### Merchant dashboard (balance + bank accounts)
```
GET /api/v1/merchants/{merchant_id}/
```

### Payout history
```
GET /api/v1/merchants/{merchant_id}/payouts/
```

### Ledger entries
```
GET /api/v1/merchants/{merchant_id}/ledger/
```

### Create payout (requires Idempotency-Key header)
```
POST /api/v1/merchants/{merchant_id}/payouts/create/
Headers:
  Content-Type: application/json
  Idempotency-Key: <uuid>
Body:
  { "amount_paise": 10000, "bank_account_id": "<uuid>" }
```

**Responses:**
- `201` — Payout created
- `400` — Missing Idempotency-Key or invalid input
- `404` — Merchant or bank account not found
- `409` — Idempotency key in flight (first request still processing)
- `422` — Insufficient funds
- `429` — Concurrent lock contention (retry after brief delay)

## Tests

```bash
cd backend
python manage.py test payouts
```

Tests cover:
- **Concurrency**: two simultaneous 60-rupee payouts on a 100-rupee balance — exactly one succeeds
- **Idempotency**: same key returns identical response, no duplicate payout
- **State machine**: illegal transitions raise ValueError
- **Balance invariant**: SUM(credits) - SUM(debits) == available_paise

## Architecture Decisions

See [EXPLAINER.md](./EXPLAINER.md) for detailed reasoning on:
- Why balance is computed from ledger rows (never stored)
- How `SELECT FOR UPDATE NOWAIT` prevents overdraw
- Idempotency with `get_or_create` and the `response_body` sentinel
- State machine implementation and enforcement
- AI-generated code that was wrong and what replaced it

## Payout Lifecycle

```
PENDING → PROCESSING → COMPLETED (70%)
                     ↘ FAILED    (20%) → funds returned to merchant atomically
[stuck > 30s] → retry with exponential backoff → FAILED after 3 attempts
```

The 10% "hang" simulation is handled by the Celery beat task that runs every 30 seconds and reschedules stuck payouts.

## Seeded Test Data

3 merchants with different balances:
| Merchant | Balance |
|---|---|
| Aryan Creative Studio | ₹4,750 |
| Priya Freelance Dev | ₹9,250 |
| Ravi Digital Agency | ₹19,500 |

(Balances after seeding, before any payouts)
