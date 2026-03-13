import pytest
import logging
from unittest.mock import MagicMock, patch
from django.db import transaction
from modules.events.event_bus import EventBus
from modules.events import events
import modules.events.tasks

@pytest.mark.django_db(transaction=True)
class TestEventBus:
    def setup_method(self):
        self.original_handlers = EventBus._handlers.copy()
        EventBus.clear()

    def teardown_method(self):
        EventBus._handlers = self.original_handlers

    def test_subscribe_and_publish(self, empresa_fixture):
        """Verify that handlers receive events with correct payload."""
        handler = MagicMock()
        EventBus.subscribe("test_event", handler)
        
        EventBus.publish("test_event", empresa_id=empresa_fixture.id, data="hello")
        
        # handler receives the Event object
        assert handler.call_count == 1
        call_event = handler.call_args[0][0]
        assert call_event.name == "test_event"
        assert call_event.payload == {"data": "hello"}
        
    def test_handler_isolation(self, empresa_fixture, caplog):
        """Verify that a failing handler does not stop other handlers."""
        def failing_handler(event):
            raise Exception("Boom!")
            
        success_handler = MagicMock()
        
        EventBus.subscribe("multi_event", failing_handler)
        EventBus.subscribe("multi_event", success_handler)
        
        # Use the specific logger name to ensure caplog catches it
        with caplog.at_level(logging.ERROR, logger="event_bus"):
            EventBus.publish("multi_event", empresa_id=empresa_fixture.id)
            
        assert success_handler.called
        assert "Event handler failed" in caplog.text

    def test_event_payload_structure(self):
        """Verify event object structure."""
        event = events.Event(
            name="struct_test",
            empresa_id="emp_1",
            usuario_id="usr_2",
            payload={"key": "value"}
        )
        
        assert event.name == "struct_test"
        assert event.empresa_id == "emp_1"
        assert event.usuario_id == "usr_2"
        assert event.payload["key"] == "value"
        assert event.event_id is not None
        assert event.timestamp is not None

    @patch("modules.events.tasks.process_event_task.apply_async")
    def test_async_dispatch_call(self, mock_apply_async, empresa_fixture):
        """Verify that when EVENT_BUS_ASYNC is True, Celery task is called via apply_async."""
        from django.conf import settings
        
        # Override setting for this test
        original_async = getattr(settings, "EVENT_BUS_ASYNC", False)
        settings.EVENT_BUS_ASYNC = True
        
        try:
            EventBus.publish("venta_confirmada", empresa_id=empresa_fixture.id)
            assert mock_apply_async.call_count == 1
            
            # Check queue routing
            kwargs = mock_apply_async.call_args[1]
            assert kwargs["queue"] == "events_high"
            
            # Check it's called with a string UUID
            args = mock_apply_async.call_args[1]["args"]
            assert isinstance(args[0], str)
        finally:
            settings.EVENT_BUS_ASYNC = original_async
