import pytest
from unittest.mock import patch
from modules.events.event_bus import EventBus
from modules.events import events
from modules.events.models import EventStore, EventStatus

@pytest.mark.django_db(transaction=True)
class TestNotificacionHandler:

    def setup_method(self):
        self.original_handlers = EventBus._handlers.copy()
        EventBus.clear()

    def teardown_method(self):
        EventBus._handlers = self.original_handlers

    @patch("modules.notificaciones.services.notificacion_service.NotificacionService.enviar_bienvenida_cliente")
    def test_cliente_creado_triggers_bienvenida(self, mock_bienvenida, empresa_fixture):
        
        # We need to manually re-subscribe since clear removes it
        import modules.events.handlers.notificacion_handler
        EventBus.subscribe(events.CLIENTE_CREADO, modules.events.handlers.notificacion_handler.handle_cliente_creado)
        
        EventBus.publish(
            events.CLIENTE_CREADO,
            empresa_id=str(empresa_fixture.id),
            cliente_id="test-cliente-123"
        )
        
        mock_bienvenida.assert_called_once_with(
            empresa_id=str(empresa_fixture.id),
            cliente_id="test-cliente-123"
        )
        
        record = EventStore.objects.first()
        assert record.status == EventStatus.PROCESSED

    @patch("modules.notificaciones.services.notificacion_service.NotificacionService.enviar_confirmacion_venta")
    def test_venta_confirmada_triggers_confirmacion(self, mock_confirmacion, empresa_fixture):
        EventBus.clear()
        import modules.events.handlers.notificacion_handler
        EventBus.subscribe(events.VENTA_CONFIRMADA, modules.events.handlers.notificacion_handler.handle_venta_confirmada)
        
        EventBus.publish(
            events.VENTA_CONFIRMADA,
            empresa_id=str(empresa_fixture.id),
            recurso_id="test-venta-123"
        )
        
        mock_confirmacion.assert_called_once_with(
            empresa_id=str(empresa_fixture.id),
            venta_id="test-venta-123"
        )

    @patch("modules.notificaciones.services.notificacion_service.NotificacionService.enviar_factura")
    def test_factura_emitida_triggers_factura(self, mock_factura, empresa_fixture):
        EventBus.clear()
        import modules.events.handlers.notificacion_handler
        EventBus.subscribe(events.FACTURA_EMITIDA, modules.events.handlers.notificacion_handler.handle_factura_emitida)
        
        EventBus.publish(
            events.FACTURA_EMITIDA,
            empresa_id=str(empresa_fixture.id),
            recurso_id="test-factura-123"
        )
        
        mock_factura.assert_called_once_with(
            empresa_id=str(empresa_fixture.id),
            factura_id="test-factura-123"
        )

    @patch("modules.notificaciones.services.notificacion_service.NotificacionService.enviar_bienvenida_cliente")
    def test_handler_failure_marks_event_as_failed(self, mock_bienvenida, empresa_fixture):
        EventBus.clear()
        import modules.events.handlers.notificacion_handler
        EventBus.subscribe(events.CLIENTE_CREADO, modules.events.handlers.notificacion_handler.handle_cliente_creado)
        
        mock_bienvenida.side_effect = Exception("Simulated service failure")
        
        EventBus.publish(
            events.CLIENTE_CREADO,
            empresa_id=str(empresa_fixture.id),
            cliente_id="test-cliente-123"
        )
        
        record = EventStore.objects.filter(event_name=events.CLIENTE_CREADO).first()
        assert record is not None
        assert record.status == EventStatus.FAILED
        assert record.retry_count == 1
        assert "Simulated service failure" in record.error_log
