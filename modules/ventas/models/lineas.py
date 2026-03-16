from django.db import models
from core.models import EmpresaModel
from .core import Venta

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
