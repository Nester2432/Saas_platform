"""
modules/ventas/models.py

Sales module models.

════════════════════════════════════════════════════════════════════════════════
Core concept: the sale as a transactional document
════════════════════════════════════════════════════════════════════════════════

A Venta is a commercial document that records what was sold, to whom, at what
price, and how it was paid. It is the authoritative record for:
    - revenue reporting
    - stock reduction (via MovimientoService)
    - invoice generation (facturacion module)
    - customer purchase history

════════════════════════════════════════════════════════════════════════════════
Model map
════════════════════════════════════════════════════════════════════════════════

    SecuenciaVenta       → per-empresa correlative counter (concurrency-safe)
    MetodoPago           → configurable payment method per empresa
    Venta                → the sale document (header)
    LineaVenta           → one line item per product/service in the sale
    PagoVenta            → one or more payment records that settle the sale
    DevolucionVenta      → a return event against a confirmed/paid sale
    DevolucionLineaVenta → one returned line item within a DevolucionVenta

════════════════════════════════════════════════════════════════════════════════
State machine
════════════════════════════════════════════════════════════════════════════════

    BORRADOR ──confirmar──► CONFIRMADA ──pagar_total──► PAGADA
        │                       │                          │
        └──cancelar──►          └──cancelar──►             └──devolver_todo──►
                            CANCELADA                              DEVUELTA

    Transitions enforced in VentaService (not as DB constraints — the full
    transition table requires Python logic that cannot be expressed as a
    single-row CHECK predicate).

    Stock is reduced at CONFIRMAR, not at BORRADOR or PAGADA.
    Stock is restored at CANCELAR (if coming from CONFIRMADA/PAGADA) and at
    DEVOLVER (proportional to the returned quantities).

════════════════════════════════════════════════════════════════════════════════
Invariants (enforced in VentaService — DB constraints where expressible)
════════════════════════════════════════════════════════════════════════════════

    V1  Totals are computed, never written manually:
            LineaVenta.subtotal = (precio_unitario × cantidad) - descuento
            Venta.subtotal      = Σ LineaVenta.subtotal
            Venta.total         = Venta.subtotal - Venta.descuento_total
    V2  Snapshots are immutable after CONFIRMADA:
            LineaVenta.precio_unitario, .descripcion, .cantidad are frozen
    V3  Stock is moved exactly once per confirmation:
            Σ SALIDA movements with ref=(venta.id) == Σ lineas with producto
    V4  Quantities returned ≤ quantities sold:
            Σ DevolucionLineaVenta.cantidad for a line ≤ LineaVenta.cantidad
    V5  numero is unique per empresa (excluding soft-deleted)
    V6  At confirmation: Σ PagoVenta.monto == Venta.total
            (can be waived for credit sales via pago_diferido=True)

════════════════════════════════════════════════════════════════════════════════
Cross-module dependency rule
════════════════════════════════════════════════════════════════════════════════

    ventas  →  inventario  (MovimientoService.registrar_salida / devolucion)
    ventas  →  clientes    (FK to Cliente — nullable)
    ventas  →  turnos      (FK to Turno — nullable)

    inventario  →  ventas  ✗  (never — inventario uses opaque referencia_tipo="venta")
    clientes    →  ventas  ✗  (never)
    turnos      →  ventas  ✗  (never)
    facturacion →  ventas  ✓  (Factura will FK to Venta — future module)

════════════════════════════════════════════════════════════════════════════════
Index strategy
════════════════════════════════════════════════════════════════════════════════

    Every index starts with `empresa` — all queries are tenant-scoped first.
    The three operationally required indexes are:
        (empresa, estado)  → "show all pending sales / all paid sales"
        (empresa, fecha)   → "sales this month / this week"
        (empresa, numero)  → "find sale by reference number"

════════════════════════════════════════════════════════════════════════════════
Correlative numbering
════════════════════════════════════════════════════════════════════════════════

    SecuenciaVenta holds one row per empresa. VentaService acquires a
    SELECT FOR UPDATE lock on that row to generate the next number atomically.
    The lock is on a tiny dedicated table — not on Venta — so concurrent
    sales don't block each other beyond the instant it takes to increment
    an integer.

    The generated format is: "V-{YYYY}-{N:04d}"  e.g. "V-2025-0001"
    Year resets are NOT automatic — the correlativo is global per empresa.
    If year-scoped sequences are needed, add a `anio` field to SecuenciaVenta.
"""

from django.core.exceptions import ValidationError
from django.db import models

from core.models import EmpresaModel


# ─────────────────────────────────────────────────────────────────────────────
# Choice enumerations
# ─────────────────────────────────────────────────────────────────────────────

class EstadoVenta(models.TextChoices):
    """
    Finite state machine for Venta lifecycle.

    Valid transitions (enforced in VentaService):

        BORRADOR ──confirmar──► CONFIRMADA ──pagar──► PAGADA
            │                       │                    │
            └────────────────────────────────────────────┴──cancelar──► CANCELADA
                                                         │
                                                         └──devolver (total)──► DEVUELTA

    Stock impact:
        BORRADOR   → no stock change
        CONFIRMADA → stock reduced (salida registered per linea with producto)
        PAGADA     → no additional stock change (already reduced at CONFIRMADA)
        CANCELADA  → stock restored IF coming from CONFIRMADA or PAGADA
        DEVUELTA   → stock restored for all returned lines

    Terminal states (no further transitions):
        CANCELADA, DEVUELTA

    Note: partial returns leave the Venta in CONFIRMADA or PAGADA state.
    Only a full return (all lines, all quantities) transitions to DEVUELTA.
    """
    BORRADOR   = "BORRADOR",   "Borrador"
    CONFIRMADA = "CONFIRMADA", "Confirmada"
    PAGADA     = "PAGADA",     "Pagada"
    CANCELADA  = "CANCELADA",  "Cancelada"
    DEVUELTA   = "DEVUELTA",   "Devuelta"


class TipoMetodoPago(models.TextChoices):
    """
    Broad category of a MetodoPago, used for business logic.

    acepta_vuelto is a MetodoPago-level field (not derived from tipo) because
    some EFECTIVO variants (e.g. foreign currency) may not give change, and
    some QR methods settle instantly like cash. The tipo here is for reporting
    groupings: "how much revenue came from cards vs cash this month".
    """
    EFECTIVO      = "EFECTIVO",      "Efectivo"
    TARJETA       = "TARJETA",       "Tarjeta (crédito / débito)"
    TRANSFERENCIA = "TRANSFERENCIA", "Transferencia bancaria"
    QR            = "QR",            "Pago QR (Mercado Pago, etc.)"
    CUENTA        = "CUENTA",        "Cuenta corriente / crédito"
    OTRO          = "OTRO",          "Otro"


# ─────────────────────────────────────────────────────────────────────────────
# SecuenciaVenta
# ─────────────────────────────────────────────────────────────────────────────

class SecuenciaVenta(EmpresaModel):
    """
    Per-empresa correlative counter for Venta.numero.

    One row per empresa, created by VentaService when the first sale is
    created (or during module activation). Never deleted.

    Concurrency contract:
        All reads and writes MUST use select_for_update() inside @transaction.atomic.
        VentaService._siguiente_numero() is the only authorised writer.

        Lock duration is minimal: one SELECT + one UPDATE on a single integer.
        The lock does NOT extend to the Venta INSERT — by the time the Venta is
        created the sequence lock has been released (same transaction, but the
        UPDATE completes immediately after the integer is incremented).

    Format produced by VentaService:
        "V-{YYYY}-{ultimo_numero:04d}"
        e.g. "V-2025-0001", "V-2025-0042"

    ultimo_numero:
        Starts at 0. After the first sale: 1. After the N-th sale: N.
        Gaps may appear if a transaction rolls back after incrementing but
        before committing — this is extremely rare and acceptable.
        To minimise gaps, _siguiente_numero() increments INSIDE the same
        @transaction.atomic as the Venta INSERT so both rollback together.
    """

    ultimo_numero = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Last used correlative number. "
            "Increment via VentaService._siguiente_numero() only."
        ),
    )

    class Meta:
        db_table            = "ventas_secuencia_venta"
        verbose_name        = "Secuencia de Venta"
        verbose_name_plural = "Secuencias de Venta"
        constraints = [
            # One sequence row per empresa — enforced at DB level.
            models.UniqueConstraint(
                fields=["empresa"],
                name="unique_secuencia_por_empresa",
            ),
        ]

    def __str__(self):
        return f"SecuenciaVenta(empresa={self.empresa_id}, ultimo={self.ultimo_numero})"


# ─────────────────────────────────────────────────────────────────────────────
# MetodoPago
# ─────────────────────────────────────────────────────────────────────────────

class MetodoPago(EmpresaModel):
    """
    A payment method configured by the empresa.

    Stored as a table (not an enum) because payment methods vary widely by
    country, industry, and business type. A pharmacy in Argentina may have
    PAMI, OSDE, cash, QR, and bank transfer. A hair salon may only need
    cash and MercadoPago. Adding a new method should not require a deploy.

    acepta_vuelto is used in POS (point-of-sale) flows to determine whether
    the cashier needs to enter the tendered amount and calculate change.
    Only EFECTIVO variants typically accept change.
    """

    nombre        = models.CharField(
        max_length=100,
        help_text="Display name, e.g. 'Efectivo', 'Tarjeta Visa', 'QR Mercado Pago'.",
    )
    tipo          = models.CharField(
        max_length=20,
        choices=TipoMetodoPago.choices,
        default=TipoMetodoPago.OTRO,
        help_text="Broad category for reporting and business logic.",
    )
    activo        = models.BooleanField(
        default=True,
        help_text="Inactive methods are hidden from the payment selection UI.",
    )
    acepta_vuelto = models.BooleanField(
        default=False,
        help_text=(
            "If True, the POS UI prompts for tendered amount and calculates change. "
            "Typically True only for EFECTIVO methods."
        ),
    )
    orden         = models.PositiveSmallIntegerField(
        default=0,
        help_text="Display order in payment selection UI.",
    )

    class Meta:
        db_table            = "ventas_metodo_pago"
        verbose_name        = "Método de Pago"
        verbose_name_plural = "Métodos de Pago"
        ordering            = ["orden", "nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["empresa", "nombre"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_metodo_pago_nombre_por_empresa",
            ),
        ]
        indexes = [
            models.Index(
                fields=["empresa", "activo", "orden"],
                name="idx_metodo_pago_empresa_activo",
            ),
        ]

    def __str__(self):
        return self.nombre


# ─────────────────────────────────────────────────────────────────────────────
# Venta
# ─────────────────────────────────────────────────────────────────────────────

class Venta(EmpresaModel):
    """
    A sale document — the central transactional entity of the ventas module.

    ── Header fields ───────────────────────────────────────────────────────────

    numero:
        Human-readable correlative reference generated by VentaService.
        Format: "V-{YYYY}-{N:04d}". Unique per empresa (partial index excludes
        soft-deleted records).

    cliente:
        Optional FK to clientes.Cliente. NULL for anonymous/walk-in sales.
        on_delete=SET_NULL: deleting a client does not delete their sale history.
        The sale's financial data is independent of the client record.

    turno:
        Optional FK to turnos.Turno. Set when a sale originates from a completed
        appointment (e.g. a spa charges for the session). NULL for direct sales.
        on_delete=SET_NULL: cancelling a turno does not affect the sale record.

    ── Financial fields (all computed by VentaService — never set manually) ──

    subtotal:
        Σ(LineaVenta.subtotal) — sum of all line items after line-level discounts.
        Stored for efficient queries ("total sales this week") without recalculating.

    descuento_total:
        Sale-level discount applied after subtotal (e.g. 10% loyalty discount).
        Line-level discounts are on LineaVenta.descuento.

    total:
        subtotal - descuento_total. The amount the customer owes.

    ── Payment state ────────────────────────────────────────────────────────────

    pago_diferido:
        If True, VentaService.confirmar_venta() does not require Σ pagos == total.
        Used for credit sales ("cuenta corriente"). The sale confirms and reduces
        stock without requiring full payment upfront.
        Default False: cash sales require full payment at confirmation.

    ── Snapshot fields ──────────────────────────────────────────────────────────

    datos_cliente:
        JSONField snapshot of client billing data at sale time. Populated from
        Cliente when cliente is set; can be overridden manually for one-off billing
        addresses. Remains readable even if the Cliente record is later deleted.

        Expected structure (not enforced at DB level — validated in VentaService):
            {
                "nombre": "...",
                "documento": "...",     # DNI / CUIT
                "email": "...",
                "telefono": "...",
                "direccion": "..."
            }

        The facturacion module will read this field (not the FK) to populate
        invoice recipient data — it should not be affected by client updates.
    """

    numero = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="Correlative reference number, e.g. 'V-2025-0001'. Set by VentaService.",
    )
    cliente = models.ForeignKey(
        "clientes.Cliente",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ventas",
        help_text="Client who made the purchase. NULL for anonymous sales.",
    )
    turno = models.ForeignKey(
        "turnos.Turno",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ventas",
        help_text=(
            "Appointment that originated this sale. "
            "NULL for direct sales not linked to a turno."
        ),
    )
    estado = models.CharField(
        max_length=15,
        choices=EstadoVenta.choices,
        default=EstadoVenta.BORRADOR,
    )
    fecha = models.DateTimeField(
        help_text="Date and time of the sale (set at creation, not at confirmation).",
    )
    subtotal = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text=(
            "Σ LineaVenta.subtotal. "
            "Computed by VentaService. Never set manually."
        ),
    )
    descuento_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text="Sale-level discount applied after line subtotals.",
    )
    total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text=(
            "subtotal - descuento_total. "
            "Computed by VentaService. Never set manually."
        ),
    )
    pago_diferido = models.BooleanField(
        default=False,
        help_text=(
            "If True, full payment is not required at confirmation. "
            "Enables credit/account sales."
        ),
    )
    datos_cliente = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Snapshot of client billing data at sale time. "
            "Readable even if the Cliente FK is later deleted. "
            "Used by facturacion module for invoice recipient."
        ),
    )
    notas = models.TextField(blank=True)

    class Meta:
        db_table            = "ventas_venta"
        verbose_name        = "Venta"
        verbose_name_plural = "Ventas"
        ordering            = ["-fecha"]
        constraints = [
            # total must be non-negative
            models.CheckConstraint(
                check=models.Q(total__gte=0),
                name="check_venta_total_no_negativo",
            ),
            # descuento_total must not exceed subtotal
            # Expressed as: descuento_total <= subtotal
            models.CheckConstraint(
                check=models.Q(descuento_total__lte=models.F("subtotal")),
                name="check_venta_descuento_lte_subtotal",
            ),
            # numero must be unique per empresa (excluding soft-deleted records)
            models.UniqueConstraint(
                fields=["empresa", "numero"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_venta_numero_por_empresa",
            ),
        ]
        indexes = [
            # ── Operational dashboard ─────────────────────────────────────────
            # "Show all CONFIRMADA sales" / "Show all BORRADOR sales"
            # Primary index for the sales management view.
            models.Index(
                fields=["empresa", "estado"],
                name="idx_venta_empresa_estado",
            ),
            # ── Date-range reporting ──────────────────────────────────────────
            # "Sales this month" / "Revenue this week"
            # Used by Reportes module and the sales dashboard date filter.
            models.Index(
                fields=["empresa", "fecha"],
                name="idx_venta_empresa_fecha",
            ),
            # ── Reference lookup ──────────────────────────────────────────────
            # "Find sale by number" (e.g. customer asks about V-2025-0042)
            models.Index(
                fields=["empresa", "numero"],
                name="idx_venta_empresa_numero",
            ),
            # ── Client history ────────────────────────────────────────────────
            # "All sales for this client" (client detail view)
            models.Index(
                fields=["empresa", "cliente", "fecha"],
                name="idx_venta_empresa_cliente",
            ),
            # ── Combined state + date ─────────────────────────────────────────
            # "All CONFIRMADA sales this month" (most common reporting query)
            models.Index(
                fields=["empresa", "estado", "fecha"],
                name="idx_venta_empresa_estado_fecha",
            ),
        ]

    @property
    def esta_pagada_completamente(self) -> bool:
        """
        True when the sum of all PagoVenta records equals or exceeds total.

        Note: reads from the related manager — may trigger a query.
        Use only in service-layer checks, not in list serializers.
        In serializers, use the `estado` field directly.
        """
        from django.db.models import Sum
        pagado = self.pagos.aggregate(total=Sum("monto"))["total"] or 0
        return pagado >= self.total

    @property
    def es_editable(self) -> bool:
        """Only BORRADOR sales accept line-item changes."""
        return self.estado == EstadoVenta.BORRADOR

    @property
    def es_terminal(self) -> bool:
        """CANCELADA and DEVUELTA accept no further transitions."""
        return self.estado in (EstadoVenta.CANCELADA, EstadoVenta.DEVUELTA)

    @property
    def permite_devolucion(self) -> bool:
        """Returns can only be registered against confirmed or paid sales."""
        return self.estado in (EstadoVenta.CONFIRMADA, EstadoVenta.PAGADA)

    def __str__(self):
        cliente_str = str(self.cliente) if self.cliente_id else "Sin cliente"
        return f"{self.numero} — {cliente_str} ({self.get_estado_display()})"


# ─────────────────────────────────────────────────────────────────────────────
# LineaVenta
# ─────────────────────────────────────────────────────────────────────────────

class LineaVenta(EmpresaModel):
    """
    One line item within a Venta.

    ── producto: nullable FK (Pattern A) ───────────────────────────────────────

    producto is a nullable FK to inventario.Producto.
        Not null: a physical product with stock. VentaService will call
                  MovimientoService.registrar_salida() for this line.
        Null:     a service, labour charge, or ad-hoc item with no inventory.
                  No stock movement is created.

    on_delete=PROTECT: a Producto with historical sales cannot be deleted.
    This is intentional — the sale record is the ground truth for revenue
    history. Use Producto.activo=False to retire a product from new sales.

    ── Snapshot fields (immutable after Venta.CONFIRMADA) ───────────────────────

    descripcion:
        ALWAYS required. If producto is set, VentaService auto-populates this
        from producto.nombre at line creation time, but it can be overridden
        (e.g. "Café 250g — promotional lot"). If producto is null, it must be
        provided by the caller.
        Remains correct even after producto.nombre is later changed.

    precio_unitario:
        Snapshot of the price at sale time. Auto-populated from
        producto.precio_venta when producto is set. Can be overridden by staff
        for manual pricing. Never changes after CONFIRMADA.

    ── orden: explicit display ordering ─────────────────────────────────────────

    Preserves the sequence in which the operator added lines to the sale.
    Without this, any ORDER BY on a UUID primary key produces arbitrary order.
    The UI and printed receipts rely on a stable, predictable line order.

    ── movimiento_stock: direct link to the ledger entry ────────────────────────

    Set by VentaService.confirmar_venta() when the salida is registered.
    Provides O(1) traceability: LineaVenta → MovimientoStock → Producto.
    Avoids the alternative of searching by referencia_id in MovimientoStock.
    """

    venta = models.ForeignKey(
        Venta,
        on_delete=models.CASCADE,
        related_name="lineas",
    )
    producto = models.ForeignKey(
        "inventario.Producto",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lineas_venta",
        help_text=(
            "Product sold. NULL for services or ad-hoc charges with no inventory. "
            "PROTECT: products with sales history cannot be deleted."
        ),
    )
    # ── Snapshot fields ──────────────────────────────────────────────────────
    descripcion = models.CharField(
        max_length=200,
        help_text=(
            "Product or service name at sale time. "
            "Always required. Auto-populated from producto.nombre when producto is set. "
            "Immutable after Venta.CONFIRMADA."
        ),
    )
    precio_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text=(
            "Unit price at sale time. Auto-populated from producto.precio_venta. "
            "Can be overridden by staff before confirmation. "
            "Immutable after Venta.CONFIRMADA."
        ),
    )
    cantidad = models.PositiveIntegerField(
        help_text="Units sold. Immutable after Venta.CONFIRMADA.",
    )
    descuento = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Line-level discount amount (not percentage). Applied before subtotal.",
    )
    subtotal = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text=(
            "(precio_unitario × cantidad) - descuento. "
            "Computed by VentaService. Never set manually."
        ),
    )
    # ── Display order ─────────────────────────────────────────────────────────
    orden = models.PositiveSmallIntegerField(
        default=0,
        help_text=(
            "Display order of this line within the sale. "
            "Preserved on receipts, invoices, and the UI. "
            "Assigned by VentaService as len(existing_lineas) at insertion time."
        ),
    )
    # ── Stock traceability ────────────────────────────────────────────────────
    movimiento_stock = models.ForeignKey(
        "inventario.MovimientoStock",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="lineas_venta",
        help_text=(
            "The MovimientoStock(SALIDA) created for this line at confirmation. "
            "NULL for service lines (no stock movement) and for BORRADOR sales. "
            "SET_NULL: losing the link does not invalidate the sale record."
        ),
    )

    class Meta:
        db_table            = "ventas_linea_venta"
        verbose_name        = "Línea de Venta"
        verbose_name_plural = "Líneas de Venta"
        ordering            = ["venta", "orden"]
        constraints = [
            # cantidad must be strictly positive
            models.CheckConstraint(
                check=models.Q(cantidad__gt=0),
                name="check_linea_venta_cantidad_positiva",
            ),
            # precio_unitario must be non-negative
            models.CheckConstraint(
                check=models.Q(precio_unitario__gte=0),
                name="check_linea_venta_precio_no_negativo",
            ),
            # descuento must not exceed the gross line amount
            # descuento <= precio_unitario × cantidad
            models.CheckConstraint(
                check=models.Q(
                    descuento__lte=models.F("precio_unitario") * models.F("cantidad")
                ),
                name="check_linea_venta_descuento_lte_bruto",
            ),
            # subtotal must be non-negative
            models.CheckConstraint(
                check=models.Q(subtotal__gte=0),
                name="check_linea_venta_subtotal_no_negativo",
            ),
        ]
        indexes = [
            # "All lines for this sale" (sale detail view — primary read path)
            models.Index(
                fields=["venta", "orden"],
                name="idx_linea_venta_orden",
            ),
            # "All sales containing this product" (product sales history)
            models.Index(
                fields=["empresa", "producto", "-created_at"],
                name="idx_linvta_emp_prod",
            ),
        ]

    @property
    def bruto(self):
        """Gross amount before discount: precio_unitario × cantidad."""
        return self.precio_unitario * self.cantidad

    def __str__(self):
        return f"{self.descripcion} × {self.cantidad} @ {self.precio_unitario}"


# ─────────────────────────────────────────────────────────────────────────────
# PagoVenta
# ─────────────────────────────────────────────────────────────────────────────

class PagoVenta(EmpresaModel):
    """
    A single payment record contributing to settling a Venta.

    A sale may be settled by one or more payments (split payment):
        $5.000 in cash + $3.000 by card = $8.000 total

    Invariant V6 (verified by VentaService.confirmar_venta()):
        Σ(PagoVenta.monto for venta) == Venta.total
        unless Venta.pago_diferido is True.

    referencia stores the payment provider's transaction ID, the card approval
    code, the transfer reference, etc. Optional — not all methods produce one.

    Payments are append-only after confirmation. To reverse a payment, create
    a DevolucionVenta (which handles the stock reversal) and issue a separate
    refund record if needed. There is no "edit payment" operation.
    """

    venta = models.ForeignKey(
        Venta,
        on_delete=models.CASCADE,
        related_name="pagos",
    )
    metodo_pago = models.ForeignKey(
        MetodoPago,
        on_delete=models.PROTECT,
        related_name="pagos",
        help_text="Payment method used for this payment record.",
    )
    monto = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text="Amount paid via this method.",
    )
    referencia = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Payment provider reference: approval code, transfer ID, QR token, etc. "
            "Optional — not all methods produce a traceable reference."
        ),
    )
    fecha = models.DateTimeField(
        help_text="Timestamp when this payment was registered.",
    )

    class Meta:
        db_table            = "ventas_pago_venta"
        verbose_name        = "Pago de Venta"
        verbose_name_plural = "Pagos de Venta"
        ordering            = ["fecha"]
        constraints = [
            # monto must be strictly positive — a zero-amount payment is meaningless
            models.CheckConstraint(
                check=models.Q(monto__gt=0),
                name="check_pago_monto_positivo",
            ),
        ]
        indexes = [
            # "All payments for this sale" (sale payment breakdown)
            models.Index(
                fields=["venta"],
                name="idx_pago_venta",
            ),
            # "All payments via this method this month" (payment method reporting)
            models.Index(
                fields=["empresa", "metodo_pago", "fecha"],
                name="idx_pago_empresa_metodo_fecha",
            ),
        ]

    def __str__(self):
        return f"{self.metodo_pago} — {self.monto} ({self.venta.numero})"


# ─────────────────────────────────────────────────────────────────────────────
# DevolucionVenta
# ─────────────────────────────────────────────────────────────────────────────

class DevolucionVenta(EmpresaModel):
    """
    A return event registered against a CONFIRMADA or PAGADA sale.

    DevolucionVenta is the header; DevolucionLineaVenta holds the individual
    returned lines (which may be a subset of the original lines, and/or
    partial quantities).

    Partial vs total returns:
        Partial: some lines or quantities returned → Venta.estado unchanged
                 (stays CONFIRMADA or PAGADA)
        Total:   all lines at full quantity returned → Venta.estado = DEVUELTA
                 (VentaService checks this after creating the devolucion)

    total_devuelto:
        Σ DevolucionLineaVenta.monto_devuelto for this devolucion.
        Computed by VentaService. Used for refund reconciliation.

    Each DevolucionVenta triggers one MovimientoStock(DEVOLUCION) per
    returned line with producto — referencing the DevolucionVenta.id, not
    the original Venta.id, so the ledger distinguishes original sales from
    returns at the movement level.
    """

    venta = models.ForeignKey(
        Venta,
        on_delete=models.PROTECT,
        related_name="devoluciones",
        help_text=(
            "The sale being returned against. "
            "PROTECT: devoluciones cannot be orphaned by sale deletion."
        ),
    )
    motivo = models.TextField(
        help_text="Reason for the return. Required for audit trail.",
    )
    total_devuelto = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text=(
            "Σ DevolucionLineaVenta.monto_devuelto. "
            "Computed by VentaService. Used for refund reconciliation."
        ),
    )
    fecha = models.DateTimeField(
        help_text="Timestamp when the return was registered.",
    )
    notas = models.TextField(blank=True)

    class Meta:
        db_table            = "ventas_devolucion_venta"
        verbose_name        = "Devolución de Venta"
        verbose_name_plural = "Devoluciones de Venta"
        ordering            = ["-fecha"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(total_devuelto__gte=0),
                name="check_devolucion_total_no_negativo",
            ),
        ]
        indexes = [
            # "All returns for this sale" (sale detail → returns tab)
            models.Index(
                fields=["venta"],
                name="idx_devolucion_venta",
            ),
            # "All returns for this empresa this month" (returns reporting)
            models.Index(
                fields=["empresa", "fecha"],
                name="idx_devolucion_empresa_fecha",
            ),
        ]

    def __str__(self):
        return f"Dev. {self.venta.numero} — {self.total_devuelto}"


# ─────────────────────────────────────────────────────────────────────────────
# DevolucionLineaVenta
# ─────────────────────────────────────────────────────────────────────────────

class DevolucionLineaVenta(EmpresaModel):
    """
    One returned line item within a DevolucionVenta.

    Tracks which LineaVenta was returned and how many units, allowing partial
    returns (e.g. buying 5 and returning 2).

    cantidad_devuelta <= linea_venta.cantidad:
        Enforced by VentaService before creating the devolucion.
        Also enforced by the CheckConstraint as a DB-level defense.
        Note: the constraint uses the stored field, not a cross-table
        comparison (not possible in SQL CHECK) — the service-level check
        is the primary enforcement.

    monto_devuelto:
        linea_venta.precio_unitario × cantidad_devuelta.
        Uses the snapshot price from the original line — not the current
        producto.precio_venta. Computed by VentaService.

    movimiento_stock:
        The MovimientoStock(DEVOLUCION) created for this returned line.
        NULL for service lines (no stock movement needed).
        referencia_tipo="devolucion_venta", referencia_id=devolucion.id
        on the movement — distinct from the original SALIDA's "venta" reference.
    """

    devolucion = models.ForeignKey(
        DevolucionVenta,
        on_delete=models.CASCADE,
        related_name="lineas",
    )
    linea_venta = models.ForeignKey(
        LineaVenta,
        on_delete=models.PROTECT,
        related_name="devoluciones",
        help_text="The original sale line being returned.",
    )
    cantidad_devuelta = models.PositiveIntegerField(
        help_text=(
            "Units being returned. "
            "Must be <= linea_venta.cantidad - already_returned. "
            "Enforced by VentaService before creation."
        ),
    )
    monto_devuelto = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text=(
            "linea_venta.precio_unitario × cantidad_devuelta. "
            "Uses the snapshot price from the original line."
        ),
    )
    movimiento_stock = models.ForeignKey(
        "inventario.MovimientoStock",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="devoluciones_linea",
        help_text=(
            "The MovimientoStock(DEVOLUCION) for this returned line. "
            "NULL for service lines with no inventory."
        ),
    )

    class Meta:
        db_table            = "ventas_devolucion_linea_venta"
        verbose_name        = "Línea de Devolución"
        verbose_name_plural = "Líneas de Devolución"
        constraints = [
            # cantidad_devuelta must be strictly positive
            models.CheckConstraint(
                check=models.Q(cantidad_devuelta__gt=0),
                name="check_devolucion_linea_cantidad_positiva",
            ),
            # monto_devuelto must be non-negative
            models.CheckConstraint(
                check=models.Q(monto_devuelto__gte=0),
                name="check_devolucion_linea_monto_no_negativo",
            ),
            # A single LineaVenta cannot appear twice in the same DevolucionVenta
            models.UniqueConstraint(
                fields=["devolucion", "linea_venta"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_devolucion_linea_por_devolucion",
            ),
        ]
        indexes = [
            # "All returned lines for this devolucion" (devolucion detail view)
            models.Index(
                fields=["devolucion"],
                name="idx_devlin_dev",
            ),
            # "All returns for this line" (how much of this line has been returned?)
            models.Index(
                fields=["linea_venta"],
                name="idx_devlin_lin_vta",
            ),
        ]

    def __str__(self):
        return (
            f"Dev. {self.devolucion_id}: "
            f"{self.linea_venta.descripcion} × {self.cantidad_devuelta}"
        )