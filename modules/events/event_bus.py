import logging
import uuid
from datetime import datetime
from typing import Callable, Dict, List
from django.db import transaction
from .events import Event

logger = logging.getLogger("event_bus")


# ── Pluggable Dispatcher Protocol ─────────────────────────────────────────────
# To swap the transport layer (e.g. Celery → Kafka/RabbitMQ), implement a class
# with a single method `dispatch(event_store_id: str)` and point
# settings.EVENT_BUS_DISPATCHER to its dotted module path.
#
# Built-in implementations:
#   • CeleryDispatcher  – the default async transport (settings.EVENT_BUS_ASYNC=True)
#   • SyncDispatcher    – runs handlers inline (default / testing)

class CeleryDispatcher:
    """Dispatches events to Celery workers via .delay()."""
    @staticmethod
    def dispatch(event_store_id: str, event: Event):
        from .tasks import process_event_task
        
        # High priority events route to 'events_high'
        high_priority_events = [
            "venta_confirmada", 
            "pago_registrado", 
            "factura_generada",
            "suscripcion_actualizada",
            "suscripcion_creada"
        ]
        
        queue = "events_high" if event.name in high_priority_events else "events_low"
        
        process_event_task.apply_async(
            args=[event_store_id],
            queue=queue
        )


class SyncDispatcher:
    """Executes handlers synchronously (used when EVENT_BUS_ASYNC=False)."""
    @staticmethod
    def dispatch(event_store_id: str, event: Event):
        from .models import EventStore
        # Use EventBus._dispatch which owns the handler lifecycle
        # We pass event + store_record_id as in the original synchronous flow
        import uuid as _uuid
        EventBus._dispatch(event, _uuid.UUID(event_store_id))


def _resolve_dispatcher():
    """
    Returns the active dispatcher instance based on settings.

    Priority:
      1. settings.EVENT_BUS_DISPATCHER (dotted path to a class/instance)
      2. settings.EVENT_BUS_ASYNC = True  → CeleryDispatcher
      3. Default                          → SyncDispatcher (testing / local dev)
    """
    from django.conf import settings
    # Allow fully-custom dispatcher via dotted path
    custom_path = getattr(settings, "EVENT_BUS_DISPATCHER", None)
    if custom_path:
        from django.utils.module_loading import import_string
        return import_string(custom_path)
    if getattr(settings, "EVENT_BUS_ASYNC", False):
        return CeleryDispatcher
    return SyncDispatcher


class EventBus:
    """
    Internal Event Bus for decoupled module communication.
    Supports synchronous dispatch with transactional safety and error isolation.
    """
    MAX_RETRIES = 5
    _handlers: Dict[str, List[Callable[[Event], None]]] = {}

    @classmethod
    def clear(cls):
        """Clear all handlers (useful for tests)."""
        cls._handlers = {}
        logger.debug("EventBus handlers cleared")

    @classmethod
    def subscribe(cls, event_name: str, handler: Callable[[Event], None]):
        """Register a handler for a specific event."""
        if event_name not in cls._handlers:
            cls._handlers[event_name] = []
        cls._handlers[event_name].append(handler)
        logger.debug("Handler registered for event: %s", event_name)

    @classmethod
    def publish(cls, event_name: str, empresa_id: str, usuario_id: str = None, event_id: str = None, **payload):
        """
        Publishes an event. Persists it in EventStore and then dispatches it.
        """
        from uuid import UUID
        from decimal import Decimal
        from django.db import IntegrityError
        from .models import EventStore, EventStatus
        from apps.empresas.models import Empresa

        # Resolve Empresa if id passed
        if isinstance(empresa_id, (str, UUID)):
            empresa = Empresa.objects.get(id=empresa_id)
        else:
            empresa = empresa_id
            empresa_id = str(empresa.id)

        # Ensure context is set during publishing
        from core.utils.tenant_context import set_current_empresa, reset_current_empresa
        token = set_current_empresa(empresa.id)

        try:
            # Sanitize payload for JSON compatibility
            sanitized_payload = {}
            for k, v in payload.items():
                if isinstance(v, UUID):
                    sanitized_payload[k] = str(v)
                elif isinstance(v, Decimal):
                    sanitized_payload[k] = float(v)
                else:
                    sanitized_payload[k] = v

            event_kwargs = {
                "name": event_name,
                "empresa_id": str(empresa_id),
                "usuario_id": str(usuario_id) if usuario_id else None,
                "payload": sanitized_payload,
            }
            if event_id:
                event_kwargs["event_id"] = str(event_id)

            event = Event(**event_kwargs)

            # 1. Persist in EventStore
            try:
                store_record = EventStore.objects.create(
                    empresa=empresa,
                    event_name=event.name,
                    event_id=event.event_id,
                    version=event.version,
                    usuario_id=event.usuario_id,
                    payload=event.payload,
                    status=EventStatus.PENDING
                )
            except IntegrityError:
                logger.warning("Duplicate event_id detected: %s. Skipping persistence.", event.event_id)
                return

            # 2. Dispatch (after DB commit to avoid publishing ghost events)
            def trigger_dispatch():
                dispatcher = _resolve_dispatcher()
                dispatcher.dispatch(str(store_record.id), event)

            if transaction.get_connection().in_atomic_block:
                transaction.on_commit(trigger_dispatch)
            else:
                trigger_dispatch()
        finally:
            reset_current_empresa(token)

    @classmethod
    def _dispatch(cls, event: Event, store_record_id: uuid.UUID):
        """Dispatches the event to all registered handlers with error isolation and state tracking."""
        from django.utils import timezone
        from .models import EventStore, EventStatus
        
        handlers = cls._handlers.get(event.name, [])
        store_record = EventStore.objects.filter(id=store_record_id).first()
        
        if not store_record:
            logger.error("EventStore record not found for id: %s", store_record_id)
            return

        # Update to PROCESSING
        store_record.status = EventStatus.PROCESSING
        store_record.save(update_fields=["status"])

        logger.info(
            "Event processing: %s (id=%s, empresa=%s)", 
            event.name, event.event_id, event.empresa_id
        )

        all_success = True
        error_msg = None

        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                all_success = False
                error_msg = str(e)
                logger.exception(
                    "Event handler failed: %s for event %s", 
                    handler.__name__ if hasattr(handler, '__name__') else str(handler),
                    event.name
                )

        # Update Final Status
        if all_success:
            store_record.status = EventStatus.PROCESSED
            store_record.processed_at = timezone.now()
            store_record.save(update_fields=["status", "processed_at"])
        else:
            store_record.retry_count += 1
            store_record.error_log = error_msg
            if store_record.retry_count >= cls.MAX_RETRIES:
                store_record.status = EventStatus.FAILED_PERMANENT
            else:
                store_record.status = EventStatus.FAILED
            store_record.save(update_fields=["status", "retry_count", "error_log"])

    @classmethod
    def replay_failed_events(cls):
        """Reprocesses events in FAILED state."""
        from .models import EventStore, EventStatus
        
        failed_events = EventStore.objects.filter(
            status=EventStatus.FAILED,
            retry_count__lt=cls.MAX_RETRIES
        )
        
        count = failed_events.count()
        if count > 0:
            logger.info("Replaying %d failed events", count)
            
        for record in failed_events:
            dispatcher = _resolve_dispatcher()
            if dispatcher is CeleryDispatcher:
                dispatcher.dispatch(str(record.id), None)
            else:
                event = Event(
                    name=record.event_name,
                    empresa_id=str(record.empresa_id),
                    usuario_id=str(record.usuario_id) if record.usuario_id else None,
                    event_id=str(record.event_id),
                    version=getattr(record, "version", "1.0"),
                    timestamp=record.created_at,
                    payload=record.payload
                )
                cls._dispatch(event, record.id)
        
        return count
