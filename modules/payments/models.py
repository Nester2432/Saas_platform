from django.db import models
from core.models import EmpresaModel

class CustomerPaymentProfile(EmpresaModel):
    """
    Vínculo entre la Empresa y el cliente en Stripe.
    """
    stripe_customer_id = models.CharField(max_length=100, unique=True)
    
    class Meta:
        verbose_name = "Perfil de Pago"
        verbose_name_plural = "Perfiles de Pago"

    def __str__(self):
        return f"{self.empresa.nombre} - {self.stripe_customer_id}"

class PaymentTransaction(EmpresaModel):
    """
    Log de transacciones individuales (Cargos, Intents).
    """
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="usd")
    status = models.CharField(max_length=50) # paid, failed, pending
    stripe_payment_intent_id = models.CharField(max_length=100, blank=True, null=True)
    
    class Meta:
        verbose_name = "Transacción de Pago"
        verbose_name_plural = "Transacciones de Pago"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.empresa.nombre} - {self.amount} {self.currency} ({self.status})"

class PaymentInvoice(EmpresaModel):
    """
    Registro local de facturas generadas por Stripe para la plataforma.
    """
    stripe_invoice_id = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=50)
    due_date = models.DateField(null=True, blank=True)
    
    class Meta:
        verbose_name = "Factura de Plataforma"
        verbose_name_plural = "Facturas de Plataforma"

    def __str__(self):
        return f"{self.stripe_invoice_id} - {self.amount}"

class ProcessedStripeEvent(models.Model):
    """
    Para asegurar idempotencia en el procesamiento de Webhooks.
    """
    stripe_event_id = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Evento Stripe Procesado"
        verbose_name_plural = "Eventos Stripe Procesados"

    def __str__(self):
        return self.stripe_event_id
