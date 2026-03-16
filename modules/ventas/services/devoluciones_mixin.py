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

class DevolucionVentaMixin:
    @staticmethod
    @transaction.atomic
    def registrar_devolucion(
        empresa,
        venta: Venta,
        items: list[dict],
        motivo: str,
        usuario=None,
    ) -> DevolucionVenta:
        """
        Register a partial or total return against a CONFIRMADA or PAGADA sale.

        items format:
            [
                {
                    "linea_venta": LineaVenta,   # the original line
                    "cantidad":    int,           # units being returned (>0)
                },
                ...
            ]

        Validates:
            - Each linea_venta belongs to venta.
            - cantidad_devuelta <= linea.cantidad - already_returned for each line.
            - No duplicate linea_venta in items.

        For each item with producto:
            MovimientoService.registrar_devolucion(
                referencia_tipo="devolucion_venta",
                referencia_id=devolucion.id
            )

        If all lines are fully returned → Venta transitions to DEVUELTA.
        Otherwise Venta stays in its current state (CONFIRMADA or PAGADA).

        Args:
            empresa: Tenant.
            venta:   The sale being returned against.
            items:   List of {"linea_venta": LineaVenta, "cantidad": int}.
            motivo:  Reason for the return (required).
            usuario: Audit user.

        Returns:
            The created DevolucionVenta with its lineas populated.

        Raises:
            TransicionVentaInvalidaError: if venta is not CONFIRMADA or PAGADA.
            DevolucionInvalidaError:      if items are invalid (wrong quantities,
                                          foreign lines, duplicates).
            ValidationError:              if motivo is empty.
        """
        if not venta.permite_devolucion:
            raise TransicionVentaInvalidaError(
                estado_actual  = venta.estado,
                estado_destino = "DEVOLUCION",
                detalle        = (
                    "Solo se pueden registrar devoluciones sobre ventas "
                    "CONFIRMADAS o PAGADAS."
                ),
            )
        VentaService._validar_tenant_venta(empresa, venta)

        if not motivo or not motivo.strip():
            raise ValidationError(
                "El motivo es obligatorio para registrar una devolución.",
                code="motivo_requerido",
            )
        if not items:
            raise DevolucionInvalidaError("La devolución debe incluir al menos un ítem.")

        # Validate items before creating anything
        VentaService._validar_items_devolucion(venta, items)

        # Create DevolucionVenta header (total_devuelto updated below)
        devolucion = DevolucionVenta.objects.create(
            empresa        = empresa,
            venta          = venta,
            motivo         = motivo.strip(),
            total_devuelto = Decimal("0"),
            fecha          = timezone.now(),
            created_by     = usuario,
            updated_by     = usuario,
        )

        total_devuelto = Decimal("0")

        for item in items:
            linea: LineaVenta = item["linea_venta"]
            cantidad: int     = item["cantidad"]

            monto_devuelto = linea.precio_unitario * cantidad
            movimiento     = None

            if linea.producto_id:
                movimiento = MovimientoService.registrar_devolucion(
                    empresa         = empresa,
                    producto        = linea.producto,
                    cantidad        = cantidad,
                    referencia_tipo = "devolucion_venta",
                    referencia_id   = devolucion.id,
                    motivo          = f"Devolución venta {venta.numero}: {motivo}",
                    usuario         = usuario,
                )

            DevolucionLineaVenta.objects.create(
                empresa           = empresa,
                devolucion        = devolucion,
                linea_venta       = linea,
                cantidad_devuelta = cantidad,
                monto_devuelto    = monto_devuelto,
                movimiento_stock  = movimiento,
                created_by        = usuario,
                updated_by        = usuario,
            )
            total_devuelto += monto_devuelto

        # Update header total
        devolucion.total_devuelto = total_devuelto
        devolucion.save(update_fields=["total_devuelto", "updated_by", "updated_at"])

        # Check if the return is total → transition to DEVUELTA
        if VentaService._es_devolucion_total(venta):
            venta.estado     = EstadoVenta.DEVUELTA
            venta.updated_by = usuario
            venta.save(update_fields=["estado", "updated_by", "updated_at"])

        logger.info(
            "DEVOLUCION: empresa=%s venta=%s devolucion=%s total=%s",
            empresa.id, venta.id, devolucion.id, total_devuelto,
        )
        return devolucion
