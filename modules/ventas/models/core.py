from django.db import models
from core.models import EmpresaModel

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

    class Meta(EmpresaModel.Meta):
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

    class Meta(EmpresaModel.Meta):
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
