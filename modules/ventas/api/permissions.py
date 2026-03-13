"""
modules/ventas/api/permissions.py

Granular access control for the ventas module endpoints.

Permission architecture — two layers:
    Layer 1 (view-level):  IsTenantAuthenticated + ModuloActivoPermission
        → set in VentaViewSet.permission_classes
        → "is this user allowed to use the ventas module at all?"

    Layer 2 (action-level): classes defined here
        → set per-action via get_permissions() in the ViewSet
        → "is this user allowed to perform THIS specific action?"

    Layer 3 (object-level): VentaObjectPermission.has_object_permission()
        → called by ViewSet.get_object() after has_permission() passes
        → "is this user allowed to access THIS specific venta?"

Permission codes (to be seeded in Permiso.codigo):
    "ventas.ver"          → list + retrieve
    "ventas.crear"        → create (BORRADOR)
    "ventas.editar"       → agregar_linea + quitar_linea actions
    "ventas.confirmar"    → confirmar action (reduces stock)
    "ventas.cancelar"     → cancelar action (restores stock)
    "ventas.pagar"        → pagar action
    "ventas.devolver"     → devolver action

Expected role → permission mapping:
    admin_empresa  → all ventas.* permissions
    cajero         → ventas.ver + ventas.crear + ventas.editar
                     + ventas.confirmar + ventas.pagar
    supervisor     → all of cajero + ventas.cancelar + ventas.devolver
    vendedor       → ventas.ver + ventas.crear + ventas.editar

Design notes:
    - Admin bypasses all granular checks (is_empresa_admin short-circuit).
    - No DB access in has_permission() — only has_object_permission() hits DB.
    - Object-level: VentaObjectPermission applies tenant guard as second layer
      (first is TenantQuerysetMixin). Defense-in-depth against URL manipulation.
"""

import logging

from rest_framework.permissions import BasePermission

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Permission code constants
# ─────────────────────────────────────────────────────────────────────────────

PERM_VER       = "ventas.ver"
PERM_CREAR     = "ventas.crear"
PERM_EDITAR    = "ventas.editar"
PERM_CONFIRMAR = "ventas.confirmar"
PERM_CANCELAR  = "ventas.cancelar"
PERM_PAGAR     = "ventas.pagar"
PERM_DEVOLVER  = "ventas.devolver"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _es_admin(user) -> bool:
    """True for empresa admins or users with rol = ADMIN."""
    from apps.usuarios.models import Usuario
    return (
        bool(getattr(user, "is_empresa_admin", False))
        or getattr(user, "rol", None) == Usuario.RolUsuario.ADMIN
    )


def _tiene_permiso(user, codigo: str) -> bool:
    """
    Check permission via rol shortcut first, then fall back to the M2M Rol/Permiso check.

    VENDEDOR shortcut: grants all ventas write permissions.
    CONTADOR shortcut: grants only read permissions (ver).
    """
    from apps.usuarios.models import Usuario
    rol = getattr(user, "rol", None)

    # VENDEDOR can do everything except devolver (supervisor only)
    if rol == Usuario.RolUsuario.VENDEDOR:
        return codigo in (
            PERM_VER, PERM_CREAR, PERM_EDITAR,
            PERM_CONFIRMAR, PERM_CANCELAR, PERM_PAGAR,
        )

    # CONTADOR can only view
    if rol == Usuario.RolUsuario.CONTADOR:
        return codigo == PERM_VER

    # Fall back to granular Rol/Permiso M2M lookup for custom roles
    try:
        return user.tiene_permiso(codigo)
    except Exception:
        logger.exception(
            "Error checking permission %s for user %s",
            codigo, getattr(user, "id", "?"),
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Action-level permission classes
# ─────────────────────────────────────────────────────────────────────────────

class PuedeVerVentas(BasePermission):
    """LIST and RETRIEVE — requires ventas.ver."""
    message = "No tiene permiso para ver ventas."

    def has_permission(self, request, view) -> bool:
        return _es_admin(request.user) or _tiene_permiso(request.user, PERM_VER)


class PuedeCrearVentas(BasePermission):
    """POST /ventas/ (BORRADOR creation) — requires ventas.crear."""
    message = "No tiene permiso para crear ventas."

    def has_permission(self, request, view) -> bool:
        return _es_admin(request.user) or _tiene_permiso(request.user, PERM_CREAR)


class PuedeEditarVentas(BasePermission):
    """
    POST /ventas/{id}/agregar_linea/ and /quitar_linea/
    Only meaningful for BORRADOR sales — the service enforces that constraint.
    Requires ventas.editar.
    """
    message = "No tiene permiso para editar líneas de venta."

    def has_permission(self, request, view) -> bool:
        return _es_admin(request.user) or _tiene_permiso(request.user, PERM_EDITAR)


class PuedeConfirmarVentas(BasePermission):
    """
    POST /ventas/{id}/confirmar/
    This action reduces stock — permission should be limited to trusted operators.
    Requires ventas.confirmar.
    """
    message = "No tiene permiso para confirmar ventas."

    def has_permission(self, request, view) -> bool:
        return _es_admin(request.user) or _tiene_permiso(request.user, PERM_CONFIRMAR)


class PuedeCancelarVentas(BasePermission):
    """
    POST /ventas/{id}/cancelar/
    This action restores stock — it is destructive in the business sense.
    Requires ventas.cancelar.
    """
    message = "No tiene permiso para cancelar ventas."

    def has_permission(self, request, view) -> bool:
        return _es_admin(request.user) or _tiene_permiso(request.user, PERM_CANCELAR)


class PuedePagarVentas(BasePermission):
    """POST /ventas/{id}/pagar/ — requires ventas.pagar."""
    message = "No tiene permiso para registrar pagos."

    def has_permission(self, request, view) -> bool:
        return _es_admin(request.user) or _tiene_permiso(request.user, PERM_PAGAR)


class PuedeDevolverVentas(BasePermission):
    """
    POST /ventas/{id}/devolver/
    Return registration restores stock and creates DevolucionVenta.
    Requires ventas.devolver (typically supervisor-only).
    """
    message = "No tiene permiso para registrar devoluciones."

    def has_permission(self, request, view) -> bool:
        return _es_admin(request.user) or _tiene_permiso(request.user, PERM_DEVOLVER)


# ─────────────────────────────────────────────────────────────────────────────
# Object-level permission
# ─────────────────────────────────────────────────────────────────────────────

class VentaObjectPermission(BasePermission):
    """
    Object-level access control for Venta instances.

    Called by get_object() after has_permission() has already passed.

    Single rule: venta.empresa_id must match request.empresa.id.
    TenantQuerysetMixin already scopes the queryset — this is a second layer
    that catches edge cases like direct URL manipulation with a valid UUID.

    There is no "user can only see their own sales" rule at this layer because
    in the SMB context all staff with ventas.ver can see all sales. If a per-
    salesperson scope is needed, add it here as a role check.
    """

    message = "No tiene permiso para acceder a esta venta."

    def has_object_permission(self, request, view, obj) -> bool:
        empresa = getattr(request, "empresa", None)
        if not empresa:
            return False
        # Tenant guard — defense-in-depth
        if str(getattr(obj, "empresa_id", None)) != str(empresa.id):
            return False
        # Admin: unrestricted within empresa
        if _es_admin(request.user):
            return True
        # Non-admin: must have at least ventas.ver
        return _tiene_permiso(request.user, PERM_VER)