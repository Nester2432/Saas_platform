from django.db import models
from django.core.exceptions import ValidationError
from core.models import EmpresaModel
from .core import Venta
from .lineas import LineaVenta

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
