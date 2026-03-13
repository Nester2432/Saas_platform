"""
Celery tasks for asynchronous event processing.

Architecture:
    EventBus.publish → EventStore (PENDING) → process_event_task.delay(id)
                                                      ↓
                                              select_for_update()
                                              PENDING → PROCESSING
                                                      ↓
                                             Execute handlers
                                                  ↓         ↓
                                             PROCESSED     FAILED
                                                            ↓ (MAX_RETRIES)
                                                    FAILED_PERMANENT (dead-letter log)

Key guarantees:
  - Only ONE worker processes a given event at a time (select_for_update skip_locked).
  - Idempotency: each handler receives a processed_event_ids cache key derived from event_id.
  - Exponential backoff: 60s, 120s, 240s, 480s, 960s (max 5 retries = EventBus.MAX_RETRIES).
  - Dead letter: FAILED_PERMANENT events emit a CRITICAL structured log for observability pipelines.
"""
import logging
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from django.core.cache import cache

from .models import EventStore, EventStatus
from .event_bus import EventBus

logger = logging.getLogger(__name__)

# Exponential backoff intervals (seconds): retry 1→60s, 2→120s, 3→240s, 4→480s, 5→960s
BACKOFF_INTERVALS = [60, 120, 240, 480, 960]
# How long to keep idempotency keys in cache (24 hours)
IDEMPOTENCY_TTL = 86_400


def _idempotency_key(event_id: str, handler_name: str) -> str:
    return f"event_handler:{event_id}:{handler_name}"


def _execute_handler_idempotent(handler, event_obj):
    """
    Execute a handler exactly once using a cache-based idempotency guard.
    If the handler was already successfully run for this event_id, skip it.
    """
    handler_name = getattr(handler, "__name__", str(handler))
    cache_key = _idempotency_key(str(event_obj.event_id), handler_name)

    # cache.add() returns True only if the key did NOT already exist (atomic add)
    acquired = cache.add(cache_key, "done", timeout=IDEMPOTENCY_TTL)
    if not acquired:
        logger.info(
            "Skipping idempotent handler (already processed)",
            extra={
                "event_id": event_obj.event_id,
                "event_name": event_obj.name,
                "handler": handler_name,
            },
        )
        return

    handler(event_obj)


@shared_task(bind=True, max_retries=EventBus.MAX_RETRIES)
def process_event_task(self, event_store_id: str):
    """
    Celery task that processes a single EventStore record asynchronously.

    Guarantees:
    - Mutex via select_for_update (skip_locked=True) – two workers never process the same event.
    - Idempotent handler execution via cache key.
    - Exponential backoff when a transient failure occurs.
    - Dead-letter logging when FAILED_PERMANENT is reached.
    """
    logger.info(
        "Worker picked up event",
        extra={"event_store_id": event_store_id, "attempt": self.request.retries + 1},
    )

    try:
        with transaction.atomic():
            # Fetch with a pessimistic lock – skip if another worker already has it.
            event_record = (
                EventStore.objects.select_for_update(skip_locked=True)
                .filter(id=event_store_id)
                .first()
            )

            if event_record is None:
                logger.warning(
                    "EventStore record not found or locked by another worker",
                    extra={"event_store_id": event_store_id},
                )
                return

            # Guard against double-processing
            if event_record.status in (
                EventStatus.PROCESSED,
                EventStatus.FAILED_PERMANENT,
                EventStatus.PROCESSING,
            ):
                logger.info(
                    "Event already in terminal/active state – skipping",
                    extra={
                        "event_id": str(event_record.event_id),
                        "event_name": event_record.event_name,
                        "status": event_record.status,
                    },
                )
                return

            # Transition → PROCESSING (prevents concurrent workers from picking it up)
            event_record.status = EventStatus.PROCESSING
            event_record.save(update_fields=["status"])

            # ── Restore Tenant Context ───────────────────────────────────────────
            from core.utils.tenant_context import set_current_empresa, reset_current_empresa
            token = set_current_empresa(event_record.empresa_id)

            try:
                # ── Rebuild Event object ──────────────────────────────────────────────
                from .events import Event  # local import to avoid circular at module level

                event_obj = Event(
                    name=event_record.event_name,
                    empresa_id=str(event_record.empresa_id),
                    usuario_id=str(event_record.usuario_id) if event_record.usuario_id else None,
                    event_id=str(event_record.event_id),
                    version=getattr(event_record, "version", "1.0"),
                    timestamp=event_record.created_at,
                    payload=event_record.payload,
                )

                # ── Execute handlers ──────────────────────────────────────────────────
                handlers = EventBus.get_handlers(event_record.event_name)
                if not handlers:
                    logger.info(
                        "No handlers registered for event – marking PROCESSED",
                        extra={
                            "event_id": str(event_record.event_id),
                            "event_name": event_record.event_name,
                        },
                    )
                else:
                    for handler in handlers:
                        _execute_handler_idempotent(handler, event_obj)

                # ── Transition → PROCESSED ────────────────────────────────────────────
                event_record.status = EventStatus.PROCESSED
                event_record.processed_at = timezone.now()
                event_record.save(update_fields=["status", "processed_at"])

                logger.info(
                    "Event processed successfully",
                    extra={
                        "event_id": str(event_record.event_id),
                        "event_name": event_record.event_name,
                        "empresa_id": str(event_record.empresa_id),
                        "retry_count": event_record.retry_count,
                        "version": getattr(event_record, "version", "1.0"),
                    },
                )
            finally:
                if token:
                    reset_current_empresa(token)
                else:
                    from core.utils.tenant_context import clear_current_empresa
                    clear_current_empresa()

    except EventStore.DoesNotExist:
        logger.error(
            "EventStore record missing",
            extra={"event_store_id": event_store_id},
        )

    except Exception as exc:
        logger.exception(
            "Error processing event – will retry with backoff",
            extra={"event_store_id": event_store_id, "error": str(exc)},
        )
        _handle_event_failure(event_store_id, str(exc))

        # Exponential backoff retry
        current_retry = self.request.retries
        if current_retry < EventBus.MAX_RETRIES:
            countdown = BACKOFF_INTERVALS[min(current_retry, len(BACKOFF_INTERVALS) - 1)]
            raise self.retry(exc=exc, countdown=countdown)
        # If we exhausted retries, failure is already handled by _handle_event_failure above.


def _handle_event_failure(event_store_id: str, error_msg: str):
    """
    Persist failure state in a *fresh* transaction (the outer one may have been rolled back).
    Emits a CRITICAL structured log when FAILED_PERMANENT is reached (dead-letter signal).
    """
    try:
        with transaction.atomic():
            record = EventStore.objects.select_for_update().get(id=event_store_id)
            record.retry_count += 1

            timestamp = timezone.now().isoformat()
            entry = f"[{timestamp}] {error_msg}"
            record.error_log = (f"{record.error_log}\n{entry}" if record.error_log else entry)

            if record.retry_count >= EventBus.MAX_RETRIES:
                record.status = EventStatus.FAILED_PERMANENT
                record.save(update_fields=["status", "retry_count", "error_log"])

                # ── Dead-letter structured log ─────────────────────────────────────
                logger.critical(
                    "DEAD LETTER: event reached FAILED_PERMANENT – manual intervention required",
                    extra={
                        "event_id": str(record.event_id),
                        "event_name": record.event_name,
                        "empresa_id": str(record.empresa_id),
                        "retry_count": record.retry_count,
                        "version": getattr(record, "version", "1.0"),
                        "last_error": error_msg,
                    },
                )
            else:
                record.status = EventStatus.FAILED
                record.save(update_fields=["status", "retry_count", "error_log"])

                logger.warning(
                    "Event processing failed – will be retried",
                    extra={
                        "event_id": str(record.event_id),
                        "event_name": record.event_name,
                        "empresa_id": str(record.empresa_id),
                        "retry_count": record.retry_count,
                        "last_error": error_msg,
                    },
                )

    except Exception as inner:
        logger.exception(
            "Critical: could not persist failure state",
            extra={"event_store_id": event_store_id, "inner_error": str(inner)},
        )
