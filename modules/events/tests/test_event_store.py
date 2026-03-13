import pytest
import uuid
from django.urls import reverse
from rest_framework import status
from modules.events.event_bus import EventBus
from modules.events.models import EventStore, EventStatus
from modules.events import events

@pytest.mark.django_db(transaction=True)
class TestEventStore:

    def setup_method(self):
        self.original_handlers = EventBus._handlers.copy()
        EventBus.clear()

    def teardown_method(self):
        EventBus._handlers = self.original_handlers

    def test_publish_creates_store_record(self, empresa_fixture):
        """Verify that publishing an event creates a PENDING record."""
        # Setup: clear handlers to avoid side effects

        
        print(f"DEBUG: Starting test_publish_creates_store_record with empresa {empresa_fixture.id}")
        EventBus.publish(
            events.VENTA_CREADA,
            empresa_id=empresa_fixture.id,
            recurso_id=str(uuid.uuid4())
        )
        print(f"DEBUG: EventStore count: {EventStore.objects.count()}")
        
        assert EventStore.objects.count() == 1
        record = EventStore.objects.first()
        assert record.event_name == events.VENTA_CREADA
        assert record.status == EventStatus.PROCESSED  # Processed because no handlers failed
        assert record.processed_at is not None

    def test_handler_failure_marks_as_failed(self, empresa_fixture):
        """Verify that a handler failure marks the record as FAILED."""
        EventBus.clear()
        
        def failing_handler(event):
            raise Exception("Simulated failure")
            
        EventBus.subscribe(events.VENTA_CREADA, failing_handler)
        
        EventBus.publish(
            events.VENTA_CREADA,
            empresa_id=empresa_fixture.id
        )
        
        record = EventStore.objects.first()
        assert record.status == EventStatus.FAILED
        assert record.retry_count == 1
        assert "Simulated failure" in record.error_log

    def test_max_retries_reaches_failed_permanent(self, empresa_fixture):
        """Verify that record reaches FAILED_PERMANENT after MAX_RETRIES."""
        EventBus.clear()
        
        def failing_handler(event):
            raise Exception("Persistent failure")
            
        EventBus.subscribe(events.VENTA_CREADA, failing_handler)
        
        # Publish initially (retry_count=1, status=FAILED)
        EventBus.publish(events.VENTA_CREADA, empresa_id=empresa_fixture.id)
        
        # Manually trigger replay 4 more times (total 5)
        for _ in range(4):
            EventBus.replay_failed_events()
            
        record = EventStore.objects.first()
        assert record.retry_count == 5
        assert record.status == EventStatus.FAILED_PERMANENT

    def test_replay_failed_events_success(self, empresa_fixture):
        """Verify that a failed event can be replayed and succeed."""
        EventBus.clear()
        
        fail = True
        def conditional_handler(event):
            nonlocal fail
            if fail:
                raise Exception("Initial failure")
            # Success on second try
            
        EventBus.subscribe(events.VENTA_CREADA, conditional_handler)
        
        # First try (fails)
        EventBus.publish(events.VENTA_CREADA, empresa_id=empresa_fixture.id)
        assert EventStore.objects.filter(status=EventStatus.FAILED).count() == 1
        
        # Fix condition and replay
        fail = False
        count = EventBus.replay_failed_events()
        
        assert count == 1
        record = EventStore.objects.first()
        assert record.status == EventStatus.PROCESSED
        assert record.retry_count == 1  # 1 failure before success

    def test_duplicate_event_id_prevention(self, empresa_fixture):
        """Verify that publishing the same event_id twice doesn't create two records."""
        EventBus.clear()
        event_id = str(uuid.uuid4())
        
        # Manipulate EventBus to use a fixed event_id for testing persistence logic
        from modules.events.events import Event
        
        def publish_with_fixed_id():
             event = Event(
                 name=events.VENTA_CREADA,
                 empresa_id=str(empresa_fixture.id),
                 event_id=event_id
             )
             # Simulate manual store creation as publish would do
             EventBus.publish(events.VENTA_CREADA, empresa_fixture.id, event_id=event_id)

        EventBus.publish(events.VENTA_CREADA, empresa_fixture.id, event_id=event_id)
        EventBus.publish(events.VENTA_CREADA, empresa_fixture.id, event_id=event_id)
        
        assert EventStore.objects.filter(event_id=event_id).count() == 1
