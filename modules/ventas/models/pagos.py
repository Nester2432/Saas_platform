from django.db import models
from core.models import EmpresaModel
from .core import Venta, MetodoPago

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
