from ..event_bus import EventBus
from ..events import VENTA_CONFIRMADA
from modules.auditlog.services.audit_service import AuditService
import logging

logger = logging.getLogger(__name__)

def handle_audit_event(event):
    """
    Subscriber that maps internal events to the Audit Log.
    """
    logger.debug("AuditHandler received: %s", event.name)
    
    from apps.empresas.models import Empresa
    from apps.usuarios.models import Usuario

    # Resolve objects (safe to do since handlers are decoupled and run on commit)
    empresa = Empresa.objects.filter(id=event.empresa_id).first()
    usuario = Usuario.objects.filter(id=event.usuario_id).first() if event.usuario_id else None

    if not empresa:
        logger.error("AuditHandler: Empresa %s not found", event.empresa_id)
        return

    # Map event names to audit actions if they differ, or use event name
    AuditService.registrar_evento(
        empresa=empresa,
        usuario=usuario,
        accion=event.name,
        recurso=event.payload.get("recurso", "system"),
        recurso_id=event.payload.get("recurso_id"),
        metadata=event.payload
    )

# Register common events that should be audited
AUDITABLE_EVENTS = [
    VENTA_CONFIRMADA,
    "venta_creada",
    "venta_cancelada",
    "factura_emitida",
    "factura_anulada",
    "producto_creado",
    "plan_cambiado",
    "login",
    "logout",
    "suscripcion_suspendida",
    "suscripcion_reactivada"
]

for event_name in AUDITABLE_EVENTS:
    EventBus.subscribe(event_name, handle_audit_event)
