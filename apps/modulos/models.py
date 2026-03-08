"""
apps/modulos/models.py

Module activation system.

Modulo: platform-wide module definitions (seeded, not created by users)
EmpresaModulo: which modules are active for a given empresa

This powers:
1. ModuloActivoPermission: rejects requests to inactive modules
2. Frontend navigation: only show active modules in the UI
3. SaaS plan gating: different plans activate different modules

Module codes (used in modulo_requerido on ViewSets):
    clientes, turnos, ventas, inventario, facturacion,
    notificaciones, reportes, ia
"""

import uuid
from django.db import models


class Modulo(models.Model):
    """
    Platform-wide module definition.

    Seeded via a Django management command or fixtures.
    Not created by individual empresas.

    Each Modulo has:
    - codigo: the string used in modulo_requerido on ViewSets
    - planes_incluidos: which SaaS plans get this module by default
    """

    class PlanMinimo(models.TextChoices):
        FREE = "free", "Free"
        STARTER = "starter", "Starter"
        PROFESSIONAL = "professional", "Professional"
        ENTERPRISE = "enterprise", "Enterprise"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nombre = models.CharField(max_length=100)
    codigo = models.SlugField(
        max_length=50,
        unique=True,
        help_text="Used in modulo_requerido. e.g. 'clientes', 'ventas'."
    )
    descripcion = models.TextField(blank=True)
    plan_minimo = models.CharField(
        max_length=20,
        choices=PlanMinimo.choices,
        default=PlanMinimo.STARTER,
        help_text="Minimum plan required to access this module."
    )
    icono = models.CharField(max_length=50, blank=True)
    orden = models.PositiveSmallIntegerField(
        default=0,
        help_text="Display order in navigation."
    )
    activo_en_plataforma = models.BooleanField(
        default=True,
        help_text="Master switch. False = disabled for all empresas."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "modulos_modulo"
        ordering = ["orden", "nombre"]
        verbose_name = "Módulo"
        verbose_name_plural = "Módulos"

    def __str__(self):
        return f"{self.nombre} ({self.codigo})"


class EmpresaModulo(models.Model):
    """
    Junction table: which modules are active for an empresa.

    Created:
    1. Automatically when an empresa upgrades their plan
    2. Manually by platform admins for custom configurations

    The activo field allows disabling a module without removing the record
    (preserves configuration history).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    empresa = models.ForeignKey(
        "empresas.Empresa",
        on_delete=models.CASCADE,
        related_name="modulos_activos",
    )
    modulo = models.ForeignKey(
        Modulo,
        on_delete=models.CASCADE,
        related_name="empresas",
    )
    activo = models.BooleanField(
        default=True,
        db_index=True,
    )
    fecha_activacion = models.DateTimeField(auto_now_add=True)
    fecha_desactivacion = models.DateTimeField(null=True, blank=True)
    activado_por = models.ForeignKey(
        "usuarios.Usuario",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "modulos_empresa_modulo"
        unique_together = [("empresa", "modulo")]
        indexes = [
            models.Index(fields=["empresa", "activo"]),
        ]
        verbose_name = "Módulo por Empresa"
        verbose_name_plural = "Módulos por Empresa"

    def __str__(self):
        estado = "activo" if self.activo else "inactivo"
        return f"{self.empresa.nombre} - {self.modulo.codigo} ({estado})"

    def desactivar(self, usuario=None):
        from django.utils import timezone
        self.activo = False
        self.fecha_desactivacion = timezone.now()
        if usuario:
            self.activado_por = usuario
        self.save(update_fields=["activo", "fecha_desactivacion", "activado_por"])
