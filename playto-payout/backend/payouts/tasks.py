"""
Playto Payout Engine - Background Tasks

process_payout: Simulates bank settlement with 70/20/10 distribution.
retry_stuck_payouts: Beat task that rescues payouts stuck in PROCESSING > 30s.

State machine transitions happen ONLY here or in views.py.
All fund returns happen atomically with the FAILED transition.
"""
import logging
import random
import time
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0)  # We manage retries manually
def process_payout(self, payout_id: str):
    """
    Picks up a PENDING payout and simulates bank settlement.

    Outcomes (seeded randomly):
    - 70% → COMPLETED (funds finalized)
    - 20% → FAILED (funds returned to merchant atomically)
    - 10% → stays in PROCESSING (will be picked up by retry_stuck_payouts)

    The PENDING → PROCESSING transition happens first, then we simulate latency.
    This means if the worker crashes mid-flight, the payout is "stuck" in PROCESSING
    and the retry beat task will rescue it.
    """
    from .models import Payout, LedgerEntry

    logger.info(f"Processing payout {payout_id}")

    try:
        with transaction.atomic():
            # Lock the payout row to prevent concurrent processing
            payout = Payout.objects.select_for_update(nowait=True).get(id=payout_id)

            if payout.status != Payout.PENDING:
                logger.warning(f"Payout {payout_id} is not PENDING (status={payout.status}), skipping.")
                return

            # PENDING → PROCESSING
            payout.transition_to(Payout.PROCESSING)

    except Payout.DoesNotExist:
        logger.error(f"Payout {payout_id} not found.")
        return
    except Exception as e:
        if 'lock' in str(e).lower():
            logger.warning(f"Could not lock payout {payout_id} — another worker has it.")
            return
        raise

    # Simulate bank network latency (outside the transaction — we're in PROCESSING now)
    time.sleep(random.uniform(0.5, 2.0))

    # Determine outcome: 70% success, 20% fail, 10% hang
    roll = random.random()

    if roll < 0.70:
        # SUCCESS path
        _complete_payout(payout_id)
    elif roll < 0.90:
        # FAILURE path — 20%
        _fail_payout(payout_id, reason="Bank rejected the transfer.")
    else:
        # HANG path — 10%
        # Leave in PROCESSING; retry_stuck_payouts will pick it up after 30s.
        logger.info(f"Payout {payout_id} simulating hang — will be retried by beat task.")


def _complete_payout(payout_id: str):
    """Transition payout to COMPLETED. Funds are already debited (held), no refund needed."""
    from .models import Payout

    try:
        with transaction.atomic():
            payout = Payout.objects.select_for_update(nowait=True).get(id=payout_id)
            if payout.status != Payout.PROCESSING:
                logger.warning(f"Payout {payout_id} no longer PROCESSING on complete attempt.")
                return
            payout.transition_to(Payout.COMPLETED)
            logger.info(f"Payout {payout_id} COMPLETED successfully.")
    except Exception as e:
        logger.exception(f"Error completing payout {payout_id}: {e}")


def _fail_payout(payout_id: str, reason: str):
    """
    Transition payout to FAILED and return funds to merchant.

    ATOMICITY: The state transition AND the credit ledger entry happen inside
    a single DB transaction. If either fails, both roll back — the merchant
    never loses money due to a partial failure.
    """
    from .models import Payout, LedgerEntry

    try:
        with transaction.atomic():
            payout = Payout.objects.select_for_update(nowait=True).get(id=payout_id)

            if payout.status != Payout.PROCESSING:
                logger.warning(f"Payout {payout_id} no longer PROCESSING on fail attempt.")
                return

            # State machine: PROCESSING → FAILED
            payout.transition_to(Payout.FAILED, failure_reason=reason)

            # Return funds: credit the merchant's ledger atomically
            LedgerEntry.objects.create(
                merchant=payout.merchant,
                entry_type=LedgerEntry.CREDIT,
                amount_paise=payout.amount_paise,
                payout=payout,
                description=f'Refund for failed payout {payout.id}: {reason}',
            )

            logger.info(f"Payout {payout_id} FAILED. ₹{payout.amount_paise / 100:.2f} returned to merchant.")
    except Exception as e:
        logger.exception(f"Error failing payout {payout_id}: {e}")


@shared_task
def retry_stuck_payouts():
    """
    Beat task: runs every 30 seconds.
    Finds payouts stuck in PROCESSING for > 30 seconds and retries or fails them.

    Exponential backoff: wait_seconds = 2^retry_attempt seconds before re-dispatching.
    Max 3 retries, then FAILED with fund return.

    Design: we collect IDs first (with skip_locked to avoid blocking active workers),
    then process each one in its own transaction. This avoids the deadlock that would
    occur if we called _fail_payout() (which does its own SELECT FOR UPDATE) from
    inside the outer locked queryset loop.
    """
    from .models import Payout
    from django.conf import settings

    threshold_seconds = getattr(settings, 'PAYOUT_STUCK_THRESHOLD_SECONDS', 30)
    max_retries = getattr(settings, 'PAYOUT_MAX_RETRIES', 3)

    cutoff = timezone.now() - timedelta(seconds=threshold_seconds)

    # Collect IDs without holding locks — each will be processed independently
    stuck_ids = list(
        Payout.objects.filter(
            status=Payout.PROCESSING,
            processing_started_at__lt=cutoff,
        ).values_list('id', 'retry_attempt')
    )

    for payout_id, retry_attempt in stuck_ids:
        logger.info(f"Found stuck payout {payout_id} (attempt {retry_attempt})")

        if retry_attempt >= max_retries:
            # Exhausted retries — give up and return funds (each in its own transaction)
            _fail_payout(str(payout_id), reason=f"Timed out after {max_retries} retry attempts.")
        else:
            # Reset to PENDING for retry in its own transaction
            with transaction.atomic():
                try:
                    payout = Payout.objects.select_for_update(nowait=True).get(
                        id=payout_id, status=Payout.PROCESSING
                    )
                    payout.retry_attempt += 1
                    payout.status = Payout.PENDING
                    payout.processing_started_at = None
                    payout.save(update_fields=['retry_attempt', 'status', 'processing_started_at', 'updated_at'])

                    backoff = 2 ** payout.retry_attempt
                    process_payout.apply_async(args=[str(payout.id)], countdown=backoff)
                    logger.info(f"Retrying payout {payout.id} in {backoff}s (attempt {payout.retry_attempt})")
                except Payout.DoesNotExist:
                    logger.info(f"Payout {payout_id} already moved out of PROCESSING — skipping.")
                except Exception as e:
                    if 'lock' in str(e).lower():
                        logger.info(f"Payout {payout_id} is locked by another worker — skipping.")
                    else:
                        logger.exception(f"Error retrying payout {payout_id}: {e}")
