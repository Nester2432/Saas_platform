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