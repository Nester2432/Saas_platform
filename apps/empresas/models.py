"""
apps/empresas/models.py

Tenant root models. Empresa is the anchor of the entire multi-tenant system.

Every piece of business data references an Empresa via EmpresaModel.

Design note:
    Empresa inherits from BaseModel (UUID PK, timestamps, soft delete, audit)
    but NOT from EmpresaModel — it cannot reference itself as a tenant.
    BaseModel gives us consistency without circular dependency.
"""

from django.db import models
from django.utils.text import slugify

from core.models import BaseModel


class Empresa(BaseModel):
    """
    Represents a company (tenant) using the SaaS platform.

    Inherits from BaseModel:
        id (UUID PK), created_at, updated_at, deleted_at,
        created_by, updated_by, soft_delete(), restore(), is_deleted

    Does NOT inherit from EmpresaModel — Empresa IS the tenant root,
    it cannot have an empresa FK pointing to itself.

    Plan field controls which modules are available to this empresa
    (enforced via EmpresaModulo records and ModuloActivoPermission).
    """



    nombre = models.CharField(max_length=200)
    slug = models.SlugField(
        max_length=100,
        unique=True,
        help_text="URL identifier, e.g. 'mi-empresa'. Auto-generated from nombre."
    )
    email = models.EmailField(help_text="Primary contact email.")
    telefono = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Inactive empresas cannot access the API."
    )
    logo_url = models.URLField(blank=True)
    zona_horaria = models.CharField(
        max_length=50,
        default="America/Argentina/Buenos_Aires",
    )
    pais = models.CharField(max_length=3, default="ARG")  # ISO 3166-1 alpha-3

    class Meta:
        db_table = "empresas_empresa"
        ordering = ["nombre"]
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["is_active"]),
        ]
        verbose_name = "Empresa"
        verbose_name_plural = "Empresas"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.nombre)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.nombre


class EmpresaConfiguracion(BaseModel):
    """
    One-to-one configuration for an Empresa.

    Kept separate from Empresa to avoid bloating the main table
    and to allow lazy loading when config is not needed.

    Inherits from BaseModel: UUID PK, created_at, updated_at, soft delete.
    Does NOT inherit EmpresaModel — uses OneToOneField instead of FK
    since there is always exactly one config per empresa.

    Stores:
    - UI/branding preferences
    - Business rules (e.g. appointment duration defaults)
    - Integration credentials (stored encrypted in production)
    """

    empresa = models.OneToOneField(
        Empresa,
        on_delete=models.CASCADE,
        related_name="configuracion",
    )
    # Branding
    color_primario = models.CharField(max_length=7, default="#3B82F6")
    color_secundario = models.CharField(max_length=7, default="#1E40AF")
    # Business rules
    duracion_turno_default = models.PositiveIntegerField(
        default=30,
        help_text="Default appointment duration in minutes."
    )
    recordatorio_horas_antes = models.PositiveIntegerField(
        default=24,
        help_text="Hours before appointment to send reminder."
    )
    permite_cancelacion_horas = models.PositiveIntegerField(
        default=2,
        help_text="Minimum hours before appointment to allow cancellation."
    )
    # Billing
    moneda = models.CharField(max_length=3, default="ARS")
    prefijo_factura = models.CharField(max_length=10, default="FAC")
    punto_venta_afip = models.PositiveIntegerField(null=True, blank=True)
    # Integrations (store tokens/keys — encrypt at field level in production)
    google_calendar_token = models.TextField(blank=True)
    whatsapp_token = models.TextField(blank=True)

    class Meta:
        db_table = "empresas_configuracion"
        verbose_name = "Configuración de Empresa"

    def __str__(self):
        return f"Config: {self.empresa.nombre}"

