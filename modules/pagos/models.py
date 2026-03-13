from django.db import models
from django.core.exceptions import ValidationError
from core.models import EmpresaModel

class EstadoPago(models.TextChoices):
    PENDIENTE   = "PENDIENTE", "Pendiente"
    CONFIRMADO  = "CONFIRMADO", "Confirmado"
    FALLIDO     = "FALLIDO", "Fallido"
    REEMBOLSADO = "REEMBOLSADO", "Reembolsado"

class Pago(EmpresaModel):
    """
    Represents a payment transaction from an external provider or internal intent.
    
    This is the orchestration entity for the payment lifecycle.
    Once confirmed, it triggers the accounting record in the sales module.
    """
    venta = models.ForeignKey(
        "ventas.Venta",
        on_delete=models.PROTECT,
        related_name="pagos_transacciones",
        help_text="The sale this payment belongs to."
    )
    monto = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text="Amount intended to be paid."
    )
    moneda = models.CharField(
        max_length=3,
        default="ARS",
        help_text="ISO 4217 currency code (e.g., ARS, USD)."
    )
    metodo_pago = models.ForeignKey(
        "ventas.MetodoPago",
        on_delete=models.PROTECT,
        related_name="pagos_transacciones",
        help_text="The payment method used for this transaction."
    )
    estado = models.CharField(
        max_length=20,
        choices=EstadoPago.choices,
        default=EstadoPago.PENDIENTE,
        db_index=True
    )
    referencia_externa = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="External transaction ID from Stripe, MercadoPago, etc."
    )
    
    class Meta:
        db_table = "pagos_pago"
        verbose_name = "Pago"
        verbose_name_plural = "Pagos"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(monto__gt=0),
                name="check_pagos_pago_monto_positivo"
            )
        ]
        indexes = [
            models.Index(fields=["empresa", "venta"]),
            models.Index(fields=["empresa", "estado"]),
            models.Index(fields=["empresa", "referencia_externa"]),
        ]

    def __str__(self):
        return f"Pago {self.id} - {self.monto} {self.moneda} ({self.get_estado_display()})"
