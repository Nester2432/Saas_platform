"""
modules/ventas/services/ventas.py

VentaService — all write operations for the ventas module.

════════════════════════════════════════════════════════════════════════════════
Contract
════════════════════════════════════════════════════════════════════════════════

Every public method:
    1. Is decorated with @transaction.atomic.
    2. Is the single authorised writer for the entities it touches.
    3. Calls MovimientoService (not models directly) for any stock change.
    4. Recalculates and persists all derived totals (subtotal, total).
    5. Returns the mutated Venta instance (refreshed from DB after writes).

No view, serializer, or task may:
    - Set Venta.total, Venta.subtotal, LineaVenta.subtotal directly.
    - Transition Venta.estado without going through this service.
    - Call MovimientoService for ventas-related stock changes.

════════════════════════════════════════════════════════════════════════════════
State machine
════════════════════════════════════════════════════════════════════════════════

    BORRADOR ──crear_venta──────────────────────────── (initial state)
    BORRADOR ──agregar_linea / quitar_linea──────────── (editable)
    BORRADOR ──confirmar_venta──► CONFIRMADA           (stock reduced here)
    CONFIRMADA ──registrar_pago, saldo==0──► PAGADA    (financial settlement)
    CONFIRMADA / PAGADA ──registrar_devolucion──►      (stock restored here)
        CONFIRMADA / PAGADA (partial) or DEVUELTA (total)
    CONFIRMADA / PAGADA ──cancelar_venta──► CANCELADA  (stock restored here)
    BORRADOR ──cancelar_venta──► CANCELADA             (no stock to restore)

════════════════════════════════════════════════════════════════════════════════
Concurrency
════════════════════════════════════════════════════════════════════════════════

Two types of concurrent writes require serialisation:

1. Correlative number generation:
       _siguiente_numero() acquires SELECT FOR UPDATE on SecuenciaVenta.
       Lock is held only for the duration of one UPDATE on one row.

2. Stock reduction (delegated to MovimientoService):
       MovimientoService.registrar_salida() acquires SELECT FOR UPDATE on
       each Producto. Each product lock is independent — selling product A
       and product B concurrently does not block each other.

       If confirmar_venta() processes N lines and line K raises
       StockInsuficienteError, the @transaction.atomic rollback undoes
       the salidas of lines 0..K-1. The sale returns to BORRADOR.

════════════════════════════════════════════════════════════════════════════════
Cross-module calls
════════════════════════════════════════════════════════════════════════════════

    MovimientoService.registrar_salida()    → called in confirmar_venta()
    MovimientoService.registrar_devolucion() → called in registrar_devolucion()
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from modules.inventario.services import MovimientoService
from modules.billing.services.billing_service import BillingService
from modules.ventas.exceptions import (
    TransicionVentaInvalidaError,
    VentaSinLineasError,
    PagoInsuficienteError,
    DevolucionInvalidaError,
)
from modules.ventas.models import (
    DevolucionLineaVenta,
    DevolucionVenta,
    EstadoVenta,
    LineaVenta,
    PagoVenta,
    SecuenciaVenta,
    Venta,
)

from modules.auditlog.services.audit_service import AuditService
from modules.events.event_bus import EventBus
from modules.events import events

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Valid state-machine transitions
# ─────────────────────────────────────────────────────────────────────────────

_TRANSICIONES_VALIDAS: set[tuple[str, str]] = {
    (EstadoVenta.BORRADOR,   EstadoVenta.CONFIRMADA),
    (EstadoVenta.BORRADOR,   EstadoVenta.CANCELADA),
    (EstadoVenta.CONFIRMADA, EstadoVenta.PAGADA),
    (EstadoVenta.CONFIRMADA, EstadoVenta.CANCELADA),
    (EstadoVenta.CONFIRMADA, EstadoVenta.DEVUELTA),
    (EstadoVenta.PAGADA,     EstadoVenta.CANCELADA),
    (EstadoVenta.PAGADA,     EstadoVenta.DEVUELTA),
}


class VentaService:
    """
    Mutation service for Venta lifecycle management.

    All methods are static — no instance state, fully thread-safe.
    All public methods are @transaction.atomic.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Public API — creation and editing (BORRADOR phase)
    # ─────────────────────────────────────────────────────────────────────────

class PagoVentaMixin:
    @staticmethod
    @transaction.atomic
    def registrar_pago(
        empresa,
        venta: Venta,
        metodo_pago,
        monto: Decimal,
        referencia: str = "",
        fecha: Optional[datetime] = None,
        usuario=None,
    ) -> PagoVenta:
        """
        Register a payment against a CONFIRMADA sale (credit/account payment).

        When Σ(pagos) reaches Venta.total, the sale automatically transitions
        to PAGADA.

        Args:
            empresa:     Tenant.
            venta:       CONFIRMADA sale receiving the payment.
            metodo_pago: MetodoPago FK.
            monto:       Payment amount. Must be > 0.
            referencia:  Optional transaction reference.
            fecha:       Payment timestamp. Defaults to now.
            usuario:     Audit user.

        Returns:
            The new PagoVenta.

        Raises:
            TransicionVentaInvalidaError: if venta is not CONFIRMADA.
            ValidationError: if monto <= 0 or exceeds outstanding balance.
        """
        if venta.estado != EstadoVenta.CONFIRMADA:
            raise TransicionVentaInvalidaError(
                estado_actual  = venta.estado,
                estado_destino = "PAGO",
                detalle        = "Solo se puede registrar pagos en ventas CONFIRMADAS.",
            )
        VentaService._validar_tenant_venta(empresa, venta)

        if monto <= 0:
            raise ValidationError(
                "El monto del pago debe ser positivo.",
                code="monto_invalido",
            )

        ya_pagado = venta.pagos.aggregate(total=Sum("monto"))["total"] or Decimal("0")
        saldo     = venta.total - ya_pagado

        if monto > saldo:
            raise ValidationError(
                f"El pago ({monto}) supera el saldo pendiente ({saldo}).",
                code="pago_excede_saldo",
            )

        pago = PagoVenta.objects.create(
            empresa     = empresa,
            venta       = venta,
            metodo_pago = metodo_pago,
            monto       = monto,
            referencia  = referencia,
            fecha       = fecha or timezone.now(),
            created_by  = usuario,
            updated_by  = usuario,
        )

        nuevo_ya_pagado = ya_pagado + monto
        if nuevo_ya_pagado >= venta.total:
            VentaService.marcar_como_pagada(empresa, venta, usuario=usuario)

        logger.info(
            "PAGO: empresa=%s venta=%s monto=%s método=%s saldo_previo=%s",
            empresa.id, venta.id, monto, metodo_pago, saldo,
        )
        return pago
    @staticmethod
    @transaction.atomic
    def marcar_como_pagada(empresa, venta: Venta, usuario=None) -> Venta:
        """
        Finalize a sale as PAGADA and publish a VENTA_PAGADA event.

        Called automatically when Σ(pagos) >= Venta.total.
        The VENTA_PAGADA event triggers auto-invoice generation via facturacion_handler.
        """
        if venta.estado == EstadoVenta.PAGADA:
            return venta

        venta.estado = EstadoVenta.PAGADA
        venta.updated_by = usuario
        venta.save(update_fields=["estado", "updated_by", "updated_at"])

        logger.info("VENTA MARCADA COMO PAGADA: empresa=%s venta=%s", empresa.id, venta.id)

        EventBus.publish(
            events.VENTA_PAGADA,
            empresa_id=empresa.id,
            usuario_id=usuario.id if usuario else None,
            recurso="venta",
            recurso_id=venta.id,
            numero=venta.numero,
            total=float(venta.total),
        )

        return venta


    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────
