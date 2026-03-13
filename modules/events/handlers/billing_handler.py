from ..event_bus import EventBus
from ..events import VENTA_CREADA, PRODUCTO_CREADO
from modules.billing.services.billing_service import BillingService
import logging

logger = logging.getLogger(__name__)

def handle_resource_creation(event):
    """
    Updates usage metrics in Billing module.
    """
    from apps.empresas.models import Empresa
    try:
        empresa = Empresa.objects.get(id=event.empresa_id)
        
        recurso = None
        if event.name == VENTA_CREADA:
            recurso = "ventas"
        elif event.name == PRODUCTO_CREADO:
            recurso = "productos"

        if recurso:
            logger.info("Registering billing usage for %s", recurso)
            BillingService.registrar_uso(empresa, recurso)
            
    except Exception:
        logger.exception("Failed to register billing usage for event %s", event.name)

EventBus.subscribe(VENTA_CREADA, handle_resource_creation)
# Note: we might need a general handler for limits check, 
# but usually limits are checked BEFORE action (synchronously).
# This handler is for INCREMENTING usage after the fact.
