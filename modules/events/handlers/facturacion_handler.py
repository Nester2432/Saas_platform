"""
facturacion_handler.py

Subscribes to VENTA_PAGADA (not VENTA_CONFIRMADA) to auto-generate invoices.
This makes the semantic clear: invoices are generated only when a sale is fully paid.
"""
import logging
from ..event_bus import EventBus
from ..events import VENTA_PAGADA
from modules.facturacion.services.facturacion import FacturaService
from modules.ventas.models import Venta

logger = logging.getLogger(__name__)


def handle_venta_pagada(event):
    """
    Thin adapter: auto-generates an invoice when a sale is fully paid.
    Any exception bubbles up so the EventBus retry mechanism activates.
    """
    empresa_id = event.empresa_id
    venta_id = event.payload.get("recurso_id")
    usuario_id = event.usuario_id

    if not venta_id:
        logger.warning("handle_venta_pagada: no recurso_id in payload, skipping")
        return

    from apps.empresas.models import Empresa
    from apps.usuarios.models import Usuario

    empresa = Empresa.objects.get(id=empresa_id)
    venta = Venta.objects.get(id=venta_id, empresa=empresa)
    usuario = Usuario.objects.filter(id=usuario_id).first() if usuario_id else None

    logger.info(
        "Auto-generating invoice for paid sale",
        extra={
            "event_id": event.event_id,
            "empresa_id": empresa_id,
            "venta_id": str(venta_id),
        },
    )

    # Raises on failure → EventBus will retry via EventStore
    factura = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=usuario)

    # Auto-emit if an active PuntoVenta exists (Happy Path for Demo)
    from modules.facturacion.models import PuntoVenta
    pv = PuntoVenta.objects.filter(empresa=empresa, activo=True).first()
    if pv:
        FacturaService.emitir_factura(empresa, factura, pv, usuario=usuario)


EventBus.subscribe(VENTA_PAGADA, handle_venta_pagada)
