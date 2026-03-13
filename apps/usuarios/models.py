"""
apps/usuarios/models.py

Custom user model and role system for multi-tenant access control.

Architecture decisions:
- AbstractBaseUser: full control, no unused Django fields (first_name etc.)
- Usuario links to Empresa at the DB level (users cannot cross tenants)
- Rol is per-empresa: company A's "admin" != company B's "admin"
- Permisos are granular strings: "clientes.ver", "ventas.crear", etc.
"""

import uuid
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone


class UsuarioManager(BaseUserManager):
    """Custom manager for Usuario."""

    def create_user(self, email, empresa, password=None, **extra_fields):
        if not email:
            raise ValueError("El email es obligatorio.")
        if not empresa:
            raise ValueError("La empresa es obligatoria.")

        email = self.normalize_email(email)
        user = self.model(email=email, empresa=empresa, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """Superuser has no empresa — platform admin only."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_platform_admin", True)

        user = self.model(email=email, empresa=None, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user


class Usuario(AbstractBaseUser, PermissionsMixin):
    """
    Custom user model for the SaaS platform.

    Key decisions:
    - email as USERNAME_FIELD (not username)
    - empresa FK: users are scoped to one company
    - is_platform_admin: Anthropic-style super admin (no empresa)
    - is_empresa_admin: admin within their empresa
    - roles: M2M to Rol, scoped to empresa
    """

    class RolUsuario(models.TextChoices):
        ADMIN = "ADMIN", "Administrador"
        VENDEDOR = "VENDEDOR", "Vendedor"
        CONTADOR = "CONTADOR", "Contador"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    nombre = models.CharField(max_length=100)
    apellido = models.CharField(max_length=100)
    empresa = models.ForeignKey(
        "empresas.Empresa",
        null=True,  # null for platform admins
        blank=True,
        on_delete=models.CASCADE,
        related_name="usuarios",
        db_index=True,
    )
    rol = models.CharField(
        max_length=20,
        choices=RolUsuario.choices,
        default=RolUsuario.VENDEDOR
    )
    roles = models.ManyToManyField(
        "Rol",
        blank=True,
        related_name="usuarios",
    )

    # Access levels
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)          # Django admin access
    is_platform_admin = models.BooleanField(default=False) # SaaS platform admin
    is_empresa_admin = models.BooleanField(default=False)  # Admin of their empresa

    # Metadata
    avatar_url = models.URLField(blank=True)
    ultimo_login_empresa = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UsuarioManager()

    class Meta:
        db_table = "usuarios_usuario"
        verbose_name = "Usuario"
        verbose_name_plural = "Usuarios"
        constraints = [
            models.UniqueConstraint(
                fields=["empresa", "email"],
                name="unique_user_email_empresa"
            )
        ]
        indexes = [
            models.Index(fields=["empresa", "is_active"]),
            models.Index(fields=["email"]),
        ]

    @property
    def nombre_completo(self):
        return f"{self.nombre} {self.apellido}".strip()

    def tiene_permiso(self, permiso_codigo):
        """
        Check if this user has a specific permission via their roles.
        Cached per-request in production via @cached_property or Redis.
        """
        if self.rol == self.RolUsuario.ADMIN:
            return True
        return self.roles.filter(
            permisos__codigo=permiso_codigo,
            empresa=self.empresa
        ).exists()

    def __str__(self):
        return f"{self.nombre_completo} <{self.email}>"


class Rol(models.Model):
    """
    Role within an empresa. Roles are empresa-scoped.

    Predefined roles: admin, manager, operador, solo_lectura
    Empresas can also create custom roles.

    Example permissions:
        clientes.ver, clientes.crear, clientes.editar, clientes.eliminar
        turnos.ver, turnos.crear, turnos.cancelar
        ventas.ver, ventas.crear
        reportes.ver
    """

    class RolPredefinido(models.TextChoices):
        ADMIN = "admin", "Administrador"
        MANAGER = "manager", "Gerente"
        OPERADOR = "operador", "Operador"
        SOLO_LECTURA = "solo_lectura", "Solo Lectura"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    empresa = models.ForeignKey(
        "empresas.Empresa",
        on_delete=models.CASCADE,
        related_name="roles",
    )
    nombre = models.CharField(max_length=100)
    codigo = models.CharField(
        max_length=50,
        help_text="Internal code, e.g. 'admin', 'operador'."
    )
    descripcion = models.TextField(blank=True)
    es_predefinido = models.BooleanField(
        default=False,
        help_text="Predefined roles cannot be deleted."
    )
    permisos = models.ManyToManyField(
        "Permiso",
        blank=True,
        related_name="roles",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "usuarios_rol"
        unique_together = [("empresa", "codigo")]
        verbose_name = "Rol"
        verbose_name_plural = "Roles"

    def __str__(self):
        return f"{self.nombre} ({self.empresa.nombre})"


class Permiso(models.Model):
    """
    Granular permission. Not empresa-scoped — permissions are platform-wide.

    Format: "<modulo>.<accion>"
    Examples: "clientes.ver", "ventas.crear", "reportes.exportar"

    Platform admins seed these when deploying new modules.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    codigo = models.CharField(
        max_length=100,
        unique=True,
        help_text="e.g. 'clientes.crear'"
    )
    nombre = models.CharField(max_length=200)
    modulo = models.CharField(
        max_length=50,
        db_index=True,
        help_text="Module this permission belongs to."
    )
    descripcion = models.TextField(blank=True)

    class Meta:
        db_table = "usuarios_permiso"
        ordering = ["modulo", "codigo"]
        verbose_name = "Permiso"
        verbose_name_plural = "Permisos"

    def __str__(self):
        return self.codigo
