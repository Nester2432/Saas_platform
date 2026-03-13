from django.db import models
from django.db.models import Q
from core.models import BaseModel, EmpresaModel

class EstadoSuscripcion(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    TRIAL = "TRIAL", "Trial"
    PAST_DUE = "PAST_DUE", "Past Due"
    CANCELED = "CANCELED", "Canceled"
    PENDING_PAYMENT = "PENDING_PAYMENT", "Pending Payment"

class PeriodoFacturacion(models.TextChoices):
    MONTHLY = "MONTHLY", "Monthly"
    ANNUAL = "ANNUAL", "Annual"

class Plan(BaseModel):
    """
    Global SaaS plans. Defines resource limits.
    """
    nombre = models.CharField(max_length=100)
    descripcion = models.TextField(blank=True)
    precio_mensual = models.DecimalField(max_digits=12, decimal_places=2)
    precio_anual = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Limits (None = Unlimited)
    max_usuarios = models.PositiveIntegerField(null=True, blank=True)
    max_clientes = models.PositiveIntegerField(null=True, blank=True)
    max_productos = models.PositiveIntegerField(null=True, blank=True)
    
    # Stripe Integration
    stripe_price_id = models.CharField(max_length=100, blank=True, null=True)
    
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Plan"
        verbose_name_plural = "Planes"
        ordering = ["precio_mensual"]

    def __str__(self):
        return self.nombre

class Suscripcion(EmpresaModel):
    """
    Links an Empresa to a Plan.
    """
    plan = models.ForeignKey(
        Plan, 
        on_delete=models.PROTECT, 
        related_name="suscripciones"
    )
    estado = models.CharField(
        max_length=20, 
        choices=EstadoSuscripcion.choices, 
        default=EstadoSuscripcion.TRIAL
    )
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField(null=True, blank=True)
    periodo_facturacion = models.CharField(
        max_length=20,
        choices=PeriodoFacturacion.choices,
        default=PeriodoFacturacion.MONTHLY
    )
    auto_renovar = models.BooleanField(default=True)
    
    # Stripe Integration
    stripe_subscription_id = models.CharField(max_length=100, blank=True, null=True)
    payment_provider = models.CharField(max_length=20, default="stripe")

    class Meta:
        verbose_name = "Suscripción"
        verbose_name_plural = "Suscripciones"
        indexes = [
            models.Index(
                fields=["empresa", "estado", "-fecha_fin"],
                name="sub_emp_est_idx"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["empresa"],
                condition=Q(estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]),
                name="unique_active_sub_emp"
            )
        ]

    def __str__(self):
        return f"{self.empresa.nombre} - {self.plan.nombre} ({self.estado})"

class UsoMensual(EmpresaModel):
    """
    Tracks monthly usage of limited resources.
    """
    mes = models.DateField(help_text="Primer día del mes (ej: 2026-03-01)")
    
    usuarios_creados = models.PositiveIntegerField(default=0)
    productos_creados = models.PositiveIntegerField(default=0)
    ventas_creadas = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Uso Mensual"
        verbose_name_plural = "Usos Mensuales"
        constraints = [
            models.UniqueConstraint(
                fields=["empresa", "mes"], 
                name="unique_usage_per_month"
            )
        ]

    def __str__(self):
        return f"{self.empresa.nombre} - {self.mes.strftime('%Y-%m')}"
