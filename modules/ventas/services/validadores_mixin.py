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

class VentaValidadoresMixin:
    @staticmethod
    def _siguiente_numero(empresa) -> str:
        """
        Generate and persist the next correlative number for this empresa.

        Uses SELECT FOR UPDATE on SecuenciaVenta to serialise concurrent calls.
        The lock is released as soon as the UPDATE commits — which happens when
        the outermost @transaction.atomic (on confirmar_venta) commits.

        Creates the SecuenciaVenta row if it does not exist yet (first sale).

        Format: "V-{YYYY}-{N:04d}"

        Args:
            empresa: The tenant for which to generate the number.

        Returns:
            A string like "V-2025-0001".
        """
        seq, _ = SecuenciaVenta.objects.get_or_create(
            empresa=empresa,
            defaults={"ultimo_numero": 0},
        )
        # Re-fetch under lock — get_or_create may have used a stale value
        seq = SecuenciaVenta.objects.select_for_update().get(empresa=empresa)
        seq.ultimo_numero += 1
        seq.save(update_fields=["ultimo_numero", "updated_at"])

        year = timezone.now().year
        return f"V-{year}-{seq.ultimo_numero:04d}"
    @staticmethod
    def _recalcular_totales(venta: Venta, usuario=None) -> None:
        """
        Recompute Venta.subtotal and Venta.total from line items.

        Must be called after any agregar_linea / quitar_linea operation.
        Also revalidates that descuento_total does not exceed the new subtotal.

        Invariant V1:
            subtotal = Σ LineaVenta.subtotal
            total    = subtotal - descuento_total
        """
        subtotal = (
            venta.lineas.aggregate(s=Sum("subtotal"))["s"] or Decimal("0")
        )
        # Clamp descuento_total to the new subtotal
        descuento = min(venta.descuento_total, subtotal)
        total     = subtotal - descuento

        venta.subtotal        = subtotal
        venta.descuento_total = descuento
        venta.total           = total
        venta.updated_by      = usuario
        venta.save(
            update_fields=[
                "subtotal", "descuento_total", "total",
                "updated_by", "updated_at",
            ]
        )
    @staticmethod
    def _validar_transicion(venta: Venta, estado_destino: str) -> None:
        """
        Enforce the state machine. Raise TransicionVentaInvalidaError if the
        (current, target) pair is not in _TRANSICIONES_VALIDAS.
        """
        par = (venta.estado, estado_destino)
        if par not in _TRANSICIONES_VALIDAS:
            raise TransicionVentaInvalidaError(
                estado_actual  = venta.estado,
                estado_destino = estado_destino,
            )
    @staticmethod
    def _validar_editable(venta: Venta) -> None:
        """Raise TransicionVentaInvalidaError if venta is not BORRADOR."""
        if venta.estado != EstadoVenta.BORRADOR:
            raise TransicionVentaInvalidaError(
                estado_actual  = venta.estado,
                estado_destino = "EDICION",
                detalle        = (
                    "Solo se pueden modificar ventas en estado BORRADOR. "
                    f"Estado actual: {venta.estado}."
                ),
            )
    @staticmethod
    def _validar_tenant_venta(empresa, venta: Venta) -> None:
        if str(venta.empresa_id) != str(empresa.id):
            raise ValidationError(
                "La venta no pertenece a esta empresa.",
                code="tenant_mismatch",
            )
    @staticmethod
    def _validar_tenant_cliente(empresa, cliente) -> None:
        if cliente is not None and str(cliente.empresa_id) != str(empresa.id):
            raise ValidationError(
                "El cliente no pertenece a esta empresa.",
                code="tenant_mismatch",
            )
    @staticmethod
    def _validar_tenant_turno(empresa, turno) -> None:
        if turno is not None and str(turno.empresa_id) != str(empresa.id):
            raise ValidationError(
                "El turno no pertenece a esta empresa.",
                code="tenant_mismatch",
            )
    @staticmethod
    def _validar_tenant_producto(empresa, producto) -> None:
        if str(producto.empresa_id) != str(empresa.id):
            raise ValidationError(
                "El producto no pertenece a esta empresa.",
                code="tenant_mismatch",
            )
    @staticmethod
    def _validar_items_devolucion(venta: Venta, items: list[dict]) -> None:
        """
        Validate all items before any DevolucionLineaVenta is created.

        Checks:
            - Each linea_venta belongs to venta.
            - No duplicate linea_venta in items.
            - cantidad > 0 for each item.
            - cantidad_devuelta <= linea.cantidad - already_returned (V4).
        """
        venta_id = venta.id
        seen_lineas: set = set()

        for item in items:
            linea: LineaVenta = item.get("linea_venta")
            cantidad: int     = item.get("cantidad")

            if linea is None or cantidad is None:
                raise DevolucionInvalidaError(
                    "Cada ítem debe tener 'linea_venta' y 'cantidad'."
                )

            if linea.venta_id != venta_id:
                raise DevolucionInvalidaError(
                    f"La línea '{linea.descripcion}' no pertenece a la venta {venta.numero}."
                )

            if not isinstance(cantidad, int) or cantidad <= 0:
                raise DevolucionInvalidaError(
                    f"La cantidad para '{linea.descripcion}' debe ser un entero positivo."
                )

            if linea.id in seen_lineas:
                raise DevolucionInvalidaError(
                    f"La línea '{linea.descripcion}' aparece duplicada en los ítems."
                )
            seen_lineas.add(linea.id)

            # V4: cantidad_devuelta <= linea.cantidad - already_returned
            ya_devuelto = (
                linea.devoluciones.aggregate(t=Sum("cantidad_devuelta"))["t"] or 0
            )
            disponible = linea.cantidad - ya_devuelto
            if cantidad > disponible:
                raise DevolucionInvalidaError(
                    f"No se pueden devolver {cantidad} unidades de "
                    f"'{linea.descripcion}'. "
                    f"Disponible para devolución: {disponible} "
                    f"(vendido: {linea.cantidad}, ya devuelto: {ya_devuelto})."
                )
    @staticmethod
    def _es_devolucion_total(venta: Venta) -> bool:
        """
        Return True if every line item has been fully returned.

        Queries DevolucionLineaVenta to sum returned quantities per line
        and compares with LineaVenta.cantidad.
        """
        lineas = list(venta.lineas.all())
        if not lineas:
            return False

        for linea in lineas:
            ya_devuelto = (
                linea.devoluciones.aggregate(t=Sum("cantidad_devuelta"))["t"] or 0
            )
            if ya_devuelto < linea.cantidad:
                return False
        return True
    @staticmethod
    def _snapshot_cliente(cliente) -> dict:
        """
        Build a billing data snapshot from a Cliente instance.

        The snapshot is stored in Venta.datos_cliente so the sale remains
        readable even if the Cliente record is later modified or deleted.
        """
        return {
            "nombre":     f"{getattr(cliente, 'nombre', '')} {getattr(cliente, 'apellido', '')}".strip(),
            "documento":  getattr(cliente, "documento", "") or "",
            "email":      getattr(cliente, "email", "") or "",
            "telefono":   getattr(cliente, "telefono", "") or "",
            "direccion":  getattr(cliente, "direccion", "") or "",
        }