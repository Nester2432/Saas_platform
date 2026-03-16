"""
modules/clientes/models.py

CRM module models. All inherit from EmpresaModel which provides:
    - empresa FK (tenant isolation)
    - UUID primary key
    - created_at, updated_at, deleted_at (soft delete)
    - created_by, updated_by (audit trail)

Model map:
    Cliente           → the core CRM record
    EtiquetaCliente   → reusable tags scoped per empresa
    ClienteEtiqueta   → M2M join between Cliente and EtiquetaCliente
    NotaCliente       → free-text notes attached to a cliente
    HistorialCliente  → immutable event log (never soft-deleted)
"""

from django.db import models
from django.core.exceptions import ValidationError

from core.models import EmpresaModel


class EtiquetaCliente(EmpresaModel):
    """
    Reusable tag/label for grouping clients within an empresa.

    Tags are empresa-scoped — two empresas can both have a tag called
    "VIP" without any collision. The (empresa, nombre) pair is unique.
    """

    nombre = models.CharField(max_length=50)
    color = models.CharField(
        max_length=7,
        default="#6B7280",
        help_text="Hex color code, e.g. '#3B82F6'."
    )

    class Meta:
        db_table = "clientes_etiqueta"
        verbose_name = "Etiqueta"
        verbose_name_plural = "Etiquetas"
        # One tag name per empresa — tags are empresa-scoped
        constraints = [
            models.UniqueConstraint(
                fields=["empresa", "nombre"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_etiqueta_nombre_por_empresa",
            )
        ]
        indexes = [
            models.Index(fields=["empresa", "nombre"]),
        ]

    def __str__(self):
        return f"{self.nombre} ({self.empresa_id})"


class Cliente(EmpresaModel):
    """
    Core CRM record. Represents a customer of an empresa.

    Uniqueness rule:
        email is unique per empresa (not globally) — two empresas can
        have a customer with the same email without conflict.
        Enforced via UniqueConstraint with a partial index that excludes
        soft-deleted records (deleted_at IS NOT NULL).

    Index strategy:
        All indexes are composite starting with `empresa` — in a
        multi-tenant DB every query is always scoped to one empresa first.
        Single-column db_index=True on email/telefono/activo is intentionally
        omitted: a composite (empresa, field) index already covers those
        lookups and is strictly more efficient than a standalone index.

    The metadata JSONField allows storing arbitrary extra data without
    schema migrations — useful for integrations (e.g. external CRM IDs,
    custom fields per empresa).
    """

    nombre = models.CharField(max_length=100)
    apellido = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    telefono = models.CharField(max_length=30, blank=True)
    fecha_nacimiento = models.DateField(null=True, blank=True)
    notas = models.TextField(
        blank=True,
        help_text="Quick internal notes. Use NotaCliente for structured notes."
    )
    activo = models.BooleanField(
        default=True,
        help_text="Inactive clients are hidden from default lists but not deleted."
    )
    etiquetas = models.ManyToManyField(
        EtiquetaCliente,
        through="ClienteEtiqueta",
        related_name="clientes",
        blank=True,
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary extra data: external IDs, custom fields, etc."
    )

    class Meta:
        db_table = "clientes_cliente"
        verbose_name = "Cliente"
        verbose_name_plural = "Clientes"
        ordering = ["apellido", "nombre"]
        constraints = [
            # email unique per empresa, only among active (non-deleted) records
            models.UniqueConstraint(
                fields=["empresa", "email"],
                condition=models.Q(deleted_at__isnull=True, email__gt=""),
                name="unique_cliente_email_por_empresa",
            )
        ]
        indexes = [
            # --- Tenant-scoped lookups (most queries start with empresa) ---
            # Filters active/inactive clients per empresa → used by list endpoint
            models.Index(fields=["empresa", "activo"], name="idx_cliente_empresa_activo"),
            # Email lookups per empresa → uniqueness checks + search
            models.Index(fields=["empresa", "email"], name="idx_cliente_empresa_email"),
            # Phone lookups per empresa → search by telefono
            models.Index(fields=["empresa", "telefono"], name="idx_cliente_empresa_telefono"),
            # Default sort order per empresa → apellido/nombre list view
            models.Index(fields=["empresa", "apellido", "nombre"], name="idx_cliente_empresa_nombre"),
            # Recency sort per empresa → created_at ordering
            # (empresa, created_at already covered by EmpresaModel.Meta.indexes)
        ]

    @property
    def nombre_completo(self):
        return f"{self.nombre} {self.apellido}".strip()

    def __str__(self):
        return self.nombre_completo


class ClienteEtiqueta(EmpresaModel):
    """
    Explicit M2M join table between Cliente and EtiquetaCliente.

    Using an explicit through model (rather than implicit M2M) gives us:
    - empresa FK for tenant isolation on the join itself
    - created_at / created_by audit on when the tag was applied
    - UniqueConstraint to prevent duplicate tags on the same client
    """

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.CASCADE,
        related_name="cliente_etiquetas",
    )
    etiqueta = models.ForeignKey(
        EtiquetaCliente,
        on_delete=models.CASCADE,
        related_name="cliente_etiquetas",
    )

    class Meta:
        db_table = "clientes_cliente_etiqueta"
        verbose_name = "Etiqueta de Cliente"
        verbose_name_plural = "Etiquetas de Clientes"
        constraints = [
            models.UniqueConstraint(
                fields=["empresa", "cliente", "etiqueta"],
                name="unique_cliente_etiqueta_por_empresa",
            )
        ]
        indexes = [
            models.Index(fields=["empresa", "cliente"]),
            models.Index(fields=["empresa", "etiqueta"]),
        ]

    def clean(self):
        """Validate that cliente and etiqueta belong to the same empresa."""
        if self.cliente_id and self.etiqueta_id:
            if self.cliente.empresa_id != self.etiqueta.empresa_id:
                raise ValidationError(
                    "El cliente y la etiqueta deben pertenecer a la misma empresa."
                )

    def __str__(self):
        return f"{self.cliente} → {self.etiqueta}"


class NotaCliente(EmpresaModel):
    """
    Structured note attached to a Cliente.

    Separate from Cliente.notas (which is a quick scratchpad).
    NotaCliente provides a timestamped, author-attributed note history.

    Notes are soft-deleteable but the deletion is always recorded in
    HistorialCliente so the audit trail remains complete.
    """

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.CASCADE,
        related_name="notas_detalle",
    )
    contenido = models.TextField()

    class Meta:
        db_table = "clientes_nota"
        verbose_name = "Nota de Cliente"
        verbose_name_plural = "Notas de Clientes"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["empresa", "cliente"]),
            models.Index(fields=["empresa", "created_at"]),
        ]

    def __str__(self):
        preview = self.contenido[:40]
        return f"Nota ({self.cliente}) — {preview}"


class HistorialCliente(EmpresaModel):
    """
    Immutable event log for a Cliente (CRM Specific).

    Every significant CRM action on a cliente (created, updated, tag added,
    note added, etc.) is recorded here automatically via ClienteService.
    
    IMPORTANT ARCHITECTURE NOTE:
    This is NOT a replacement for the global `AuditLog` (modules/auditlog).
    `AuditLog` tracks low-level HTTP requests and security events across the SaaS.
    `HistorialCliente` is a pure business-domain feature used to render the
    "Customer Activity Timeline" widget in the CRM view.

    Design decisions:
    - Never soft-deleted: the CRM timeline must be permanent
    - metadata JSONField: each event type can store relevant context
      (e.g. UPDATED stores {"campos_modificados": ["email", "telefono"]})
    - No updated_by/updated_at make sense here — events are write-once
    """

    class TipoEvento(models.TextChoices):
        CREATED = "CREATED", "Cliente creado"
        UPDATED = "UPDATED", "Cliente actualizado"
        TAG_ADDED = "TAG_ADDED", "Etiqueta agregada"
        TAG_REMOVED = "TAG_REMOVED", "Etiqueta removida"
        NOTE_ADDED = "NOTE_ADDED", "Nota agregada"
        NOTE_DELETED = "NOTE_DELETED", "Nota eliminada"
        ACTIVATED = "ACTIVATED", "Cliente activado"
        DEACTIVATED = "DEACTIVATED", "Cliente desactivado"
        DELETED = "DELETED", "Cliente eliminado"
        RESTORED = "RESTORED", "Cliente restaurado"

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.CASCADE,
        related_name="historial",
    )
    tipo_evento = models.CharField(
        max_length=20,
        choices=TipoEvento.choices,
        db_index=True,
    )
    descripcion = models.CharField(
        max_length=500,
        help_text="Human-readable description of what happened."
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Event-specific context (e.g. changed fields, tag name)."
    )

    class Meta:
        db_table = "clientes_historial"
        verbose_name = "Historial de Cliente"
        verbose_name_plural = "Historial de Clientes"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["empresa", "cliente"]),
            models.Index(fields=["empresa", "tipo_evento"]),
            models.Index(fields=["empresa", "cliente", "created_at"]),
        ]

    def delete(self, *args, **kwargs):
        """Historial is immutable — hard deletes are blocked."""
        raise ValidationError(
            "HistorialCliente records are immutable and cannot be deleted."
        )

    def __str__(self):
        return f"{self.tipo_evento} — {self.cliente} @ {self.created_at:%Y-%m-%d %H:%M}"

