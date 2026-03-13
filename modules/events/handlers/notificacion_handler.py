import logging
from modules.events.event_bus import EventBus
from modules.events import events
from modules.notificaciones.services.notificacion_service import NotificacionService

logger = logging.getLogger(__name__)

def handle_cliente_creado(event):
    """Handles CLIENTE_CREADO event to send welcome notification."""
    try:
        NotificacionService.enviar_bienvenida_cliente(
            empresa_id=event.empresa_id,
            cliente_id=event.payload["cliente_id"]
        )
    except Exception as e:
        logger.exception("Failed to send welcome notification for client %s", event.payload.get("cliente_id"))
        raise  # Propagate to EventBus for retry mechanism

def handle_venta_confirmada(event):
    """Handles VENTA_CONFIRMADA event to send confirmation notification."""
    venta_id = event.payload.get("recurso_id")
    if not venta_id:
        return
        
    try:
        NotificacionService.enviar_confirmacion_venta(
            empresa_id=event.empresa_id,
            venta_id=venta_id
        )
    except Exception as e:
        logger.exception("Failed to send confirmation notification for venta %s", venta_id)
        raise

def handle_factura_emitida(event):
    """Handles FACTURA_EMITIDA event to send invoice notification."""
    factura_id = event.payload.get("recurso_id")
    if not factura_id:
        return
        
    try:
        NotificacionService.enviar_factura(
            empresa_id=event.empresa_id,
            factura_id=factura_id
        )
    except Exception as e:
        logger.exception("Failed to send invoice notification for factura %s", factura_id)
        raise

# Suscribir handlers a eventos
EventBus.subscribe(events.CLIENTE_CREADO, handle_cliente_creado)
EventBus.subscribe(events.VENTA_CONFIRMADA, handle_venta_confirmada)
EventBus.subscribe(events.FACTURA_EMITIDA, handle_factura_emitida)
