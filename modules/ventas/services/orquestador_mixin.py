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

class VentaOrquestadorMixin:
    @staticmethod
    @transaction.atomic
    def crear_venta(
        empresa,
        fecha: Optional[datetime] = None,
        cliente=None,
        turno=None,
        descuento_total: Decimal = Decimal("0"),
        pago_diferido: bool = False,
        notas: str = "",
        usuario=None,
    ) -> Venta:
        """
        Create a new sale in BORRADOR state.

        A BORRADOR sale is a draft — it has no correlative number yet,
        affects no stock, and can be freely edited. The number is assigned
        only at confirmar_venta().

        Args:
            empresa:         Tenant.
            fecha:           Sale date. Defaults to now.
            cliente:         Optional FK to clientes.Cliente.
            turno:           Optional FK to turnos.Turno.
            descuento_total: Sale-level discount (applied to the subtotal).
            pago_diferido:   If True, confirmation does not require full payment.
            notas:           Internal notes.
            usuario:         Audit user.

        Returns:
            New Venta in BORRADOR state, with no lineas yet.
        """
        VentaService._validar_tenant_cliente(empresa, cliente)
        VentaService._validar_tenant_turno(empresa, turno)

        if descuento_total < 0:
            raise ValidationError(
                "El descuento no puede ser negativo.",
                code="descuento_invalido",
            )

        # Billing check
        BillingService.verificar_limite(empresa, "ventas")

        venta = Venta.objects.create(
            empresa         = empresa,
            estado          = EstadoVenta.BORRADOR,
            fecha           = fecha or timezone.now(),
            cliente         = cliente,
            turno           = turno,
            descuento_total = descuento_total,
            pago_diferido   = pago_diferido,
            notas           = notas,
            subtotal        = Decimal("0"),
            total           = Decimal("0"),
            created_by      = usuario,
            updated_by      = usuario,
        )

        if cliente:
            venta.datos_cliente = VentaService._snapshot_cliente(cliente)
            venta.save(update_fields=["datos_cliente"])

        EventBus.publish(
            events.VENTA_CREADA,
            empresa_id=empresa.id,
            usuario_id=usuario.id if usuario else None,
            recurso="venta",
            recurso_id=venta.id,
            cliente_id=str(cliente.id) if cliente else None
        )
        
        return venta
    @staticmethod
    @transaction.atomic
    def agregar_linea(
        empresa,
        venta: Venta,
        descripcion: str = "",
        precio_unitario: Optional[Decimal] = None,
        cantidad: int = 1,
        producto=None,
        descuento: Decimal = Decimal("0"),
        usuario=None,
    ) -> LineaVenta:
        """
        Add a line item to a BORRADOR sale.

        Rules:
            - Only BORRADOR sales accept new lines.
            - If producto is provided, descripcion defaults to producto.nombre
              and precio_unitario defaults to producto.precio_venta.
            - descripcion is always required (either explicit or from producto).
            - precio_unitario is always required (either explicit or from producto).
            - orden is assigned as the current line count (0-indexed).

        Args:
            empresa:         Tenant.
            venta:           The BORRADOR sale receiving the line.
            descripcion:     Line description. Auto-populated from producto if omitted.
            precio_unitario: Unit price. Auto-populated from producto if omitted.
            cantidad:        Units sold. Must be > 0.
            producto:        Optional inventario.Producto FK.
            descuento:       Line-level discount amount.
            usuario:         Audit user.

        Returns:
            The new LineaVenta. Venta totals are recalculated.

        Raises:
            TransicionVentaInvalidaError: if venta is not BORRADOR.
            ValidationError: if descripcion or precio_unitario are missing/invalid.
        """
        VentaService._validar_editable(venta)
        VentaService._validar_tenant_venta(empresa, venta)

        # Auto-populate from producto if not explicitly provided
        if producto is not None:
            VentaService._validar_tenant_producto(empresa, producto)
            if not descripcion:
                descripcion = producto.nombre
            if precio_unitario is None:
                precio_unitario = producto.precio_venta or Decimal("0")

        if not descripcion or not descripcion.strip():
            raise ValidationError(
                "La descripción de la línea es obligatoria.",
                code="descripcion_requerida",
            )
        if precio_unitario is None or precio_unitario < 0:
            raise ValidationError(
                "El precio unitario es obligatorio y debe ser no negativo.",
                code="precio_invalido",
            )
        if not isinstance(cantidad, int) or cantidad <= 0:
            raise ValidationError(
                f"La cantidad debe ser un entero positivo. Recibido: {cantidad!r}",
                code="cantidad_invalida",
            )
        if descuento < 0:
            raise ValidationError(
                "El descuento de línea no puede ser negativo.",
                code="descuento_invalido",
            )

        bruto = precio_unitario * cantidad
        if descuento > bruto:
            raise ValidationError(
                f"El descuento ({descuento}) no puede superar el importe bruto "
                f"de la línea ({bruto}).",
                code="descuento_excede_bruto",
            )

        orden = venta.lineas.count()
        subtotal_linea = bruto - descuento

        linea = LineaVenta.objects.create(
            empresa         = empresa,
            venta           = venta,
            producto        = producto,
            descripcion     = descripcion.strip(),
            precio_unitario = precio_unitario,
            cantidad        = cantidad,
            descuento       = descuento,
            subtotal        = subtotal_linea,
            orden           = orden,
            created_by      = usuario,
            updated_by      = usuario,
        )

        VentaService._recalcular_totales(venta, usuario=usuario)

        return linea
    @staticmethod
    @transaction.atomic
    def quitar_linea(empresa, venta: Venta, linea: LineaVenta, usuario=None) -> None:
        """
        Remove a line item from a BORRADOR sale.

        Renumbers the remaining lines to fill the gap (keeps orden contiguous).

        Raises:
            TransicionVentaInvalidaError: if venta is not BORRADOR.
            ValidationError: if linea does not belong to venta.
        """
        VentaService._validar_editable(venta)
        VentaService._validar_tenant_venta(empresa, venta)

        if linea.venta_id != venta.id:
            raise ValidationError(
                "La línea no pertenece a esta venta.",
                code="linea_incorrecta",
            )

        linea_orden = linea.orden
        linea.delete()

        # Re-sequence lines after the removed one to keep orden contiguous
        venta.lineas.filter(orden__gt=linea_orden).update(
            orden=models.F("orden") - 1
        )

        VentaService._recalcular_totales(venta, usuario=usuario)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API — state transitions
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    @transaction.atomic
    def confirmar_venta(
        empresa,
        venta: Venta,
        pagos: Optional[list[dict]] = None,
        usuario=None,
    ) -> Venta:
        """
        Confirm a BORRADOR sale: reduce stock, assign correlativo, register payments.

        This is the most critical operation in the module. Everything happens
        in one @transaction.atomic block:

            Step 1 — Validate state and preconditions
            Step 2 — Validate lines (at least one, each with valid data)
            Step 3 — Assign correlativo number (SELECT FOR UPDATE on SecuenciaVenta)
            Step 4 — Reduce stock for each line with producto
                      (SELECT FOR UPDATE on each Producto via MovimientoService)
            Step 5 — Register payments (if provided)
            Step 6 — Validate payment sum (unless pago_diferido)
            Step 7 — Transition state to CONFIRMADA or PAGADA

        If any step fails, the entire transaction rolls back:
            - No stock is reduced
            - No correlativo is consumed (SecuenciaVenta rolls back too)
            - No payments are registered
            - Venta remains BORRADOR

        Args:
            empresa: Tenant.
            venta:   BORRADOR sale to confirm.
            pagos:   Optional list of payment dicts:
                         [{"metodo_pago": MetodoPago, "monto": Decimal,
                           "referencia": str, "fecha": datetime}, ...]
            usuario: Audit user.

        Returns:
            Venta in CONFIRMADA or PAGADA state.

        Raises:
            TransicionVentaInvalidaError: if venta is not BORRADOR.
            VentaSinLineasError:          if venta has no line items.
            StockInsuficienteError:       if any product lacks sufficient stock.
            PagoInsuficienteError:        if pagos do not cover total
                                          (and not pago_diferido).
        """
        VentaService._validar_transicion(venta, EstadoVenta.CONFIRMADA)
        VentaService._validar_tenant_venta(empresa, venta)

        # Step 2 — validate lines
        lineas = list(
            venta.lineas
            .select_related("producto")
            .order_by("orden")
        )
        if not lineas:
            raise VentaSinLineasError(venta)

        # Step 3 — assign correlativo
        numero = VentaService._siguiente_numero(empresa)
        venta.numero = numero

        # Step 4 — reduce stock
        # All salidas are inside this @transaction.atomic block.
        # If line K fails, lines 0..K-1 roll back automatically.
        for linea in lineas:
            if linea.producto_id:
                movimiento = MovimientoService.registrar_salida(
                    empresa         = empresa,
                    producto        = linea.producto,
                    cantidad        = linea.cantidad,
                    motivo          = f"Venta {numero}",
                    referencia_tipo = "venta",
                    referencia_id   = venta.id,
                    usuario         = usuario,
                )
                linea.movimiento_stock = movimiento
                linea.save(update_fields=["movimiento_stock", "updated_by", "updated_at"])

        # Step 5 — register payments
        total_pagado = Decimal("0")
        if pagos:
            for pago_data in pagos:
                PagoVenta.objects.create(
                    empresa     = empresa,
                    venta       = venta,
                    metodo_pago = pago_data["metodo_pago"],
                    monto       = pago_data["monto"],
                    referencia  = pago_data.get("referencia", ""),
                    fecha       = pago_data.get("fecha", timezone.now()),
                    created_by  = usuario,
                    updated_by  = usuario,
                )
                total_pagado += pago_data["monto"]

        # Step 6 — validate payment coverage
        if not venta.pago_diferido:
            if total_pagado < venta.total:
                raise PagoInsuficienteError(
                    total    = venta.total,
                    pagado   = total_pagado,
                    faltante = venta.total - total_pagado,
                )

        # Step 7 — transition state
        nuevo_estado = (
            EstadoVenta.PAGADA
            if total_pagado >= venta.total
            else EstadoVenta.CONFIRMADA
        )
        venta.estado     = nuevo_estado
        venta.updated_by = usuario
        venta.save(update_fields=["numero", "estado", "updated_by", "updated_at"])

        EventBus.publish(
            events.VENTA_CONFIRMADA,
            empresa_id=empresa.id,
            usuario_id=usuario.id if usuario else None,
            recurso="venta",
            recurso_id=venta.id,
            numero=numero,
            total=float(venta.total),
            estado=nuevo_estado
        )

        # If the sale was fully paid at confirmation time, also publish VENTA_PAGADA
        # (triggers auto-invoice via facturacion_handler)
        if nuevo_estado == EstadoVenta.PAGADA:
            EventBus.publish(
                events.VENTA_PAGADA,
                empresa_id=empresa.id,
                usuario_id=usuario.id if usuario else None,
                recurso="venta",
                recurso_id=venta.id,
                numero=numero,
                total=float(venta.total),
            )

        return venta

    @staticmethod
    @transaction.atomic
    def cancelar_venta(
        empresa,
        venta: Venta,
        motivo: str = "",
        usuario=None,
    ) -> Venta:
        """
        Cancel a sale. Restores stock if the sale was already CONFIRMADA or PAGADA.

        BORRADOR → CANCELADA: no stock to restore, just a state change.
        CONFIRMADA / PAGADA → CANCELADA: one DEVOLUCION movement per line
            with producto, undoing every SALIDA registered at confirmation.

        Args:
            empresa: Tenant.
            venta:   Sale to cancel. Must be BORRADOR, CONFIRMADA, or PAGADA.
            motivo:  Reason for cancellation.
            usuario: Audit user.

        Returns:
            Venta in CANCELADA state.

        Raises:
            TransicionVentaInvalidaError: if venta is terminal (CANCELADA/DEVUELTA).
        """
        VentaService._validar_transicion(venta, EstadoVenta.CANCELADA)
        VentaService._validar_tenant_venta(empresa, venta)

        stock_confirmado = venta.estado in (
            EstadoVenta.CONFIRMADA, EstadoVenta.PAGADA
        )

        if stock_confirmado:
            lineas = list(
                venta.lineas
                .select_related("producto")
                .filter(producto__isnull=False)
            )
            for linea in lineas:
                MovimientoService.registrar_devolucion(
                    empresa         = empresa,
                    producto        = linea.producto,
                    cantidad        = linea.cantidad,
                    referencia_tipo = "cancelacion_venta",
                    referencia_id   = venta.id,
                    motivo          = f"Cancelación venta {venta.numero}: {motivo}",
                    usuario         = usuario,
                )

        venta.estado     = EstadoVenta.CANCELADA
        venta.notas      = f"{venta.notas}\n[CANCELADA] {motivo}".strip()
        venta.updated_by = usuario
        venta.save(update_fields=["estado", "notas", "updated_by", "updated_at"])

        EventBus.publish(
            events.VENTA_CANCELADA,
            empresa_id=empresa.id,
            usuario_id=usuario.id if usuario else None,
            recurso="venta",
            recurso_id=venta.id,
            numero=venta.numero,
            motivo=motivo
        )

        return venta
