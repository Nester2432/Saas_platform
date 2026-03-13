"""
modules/inventario/api/permissions.py

Permission classes for the inventario module.

Role strategy:
    ADMIN     → full CRUD on all inventario resources
    VENDEDOR  → full CRUD on productos (they manage the product catalog daily)
    CONTADOR  → read-only (GET, HEAD, OPTIONS only)

These classes work alongside IsTenantAuthenticated (set in DEFAULT_PERMISSION_CLASSES).
Object-level guard (InventarioObjectPermission) is kept as a secondary defense layer.
"""
from rest_framework.permissions import BasePermission, SAFE_METHODS
from apps.usuarios.models import Usuario


class InventarioRolPermission(BasePermission):
    """
    View-level role check for inventario module endpoints.

    ADMIN    → everything
    VENDEDOR → everything (product managers)
    CONTADOR → read-only (reporting & reconciliation)
    """

    message = "No tiene permiso para realizar esta operación en el inventario."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False

        rol = getattr(user, "rol", None)

        # ADMIN has unrestricted access
        if rol == Usuario.RolUsuario.ADMIN or getattr(user, "is_empresa_admin", False):
            return True

        # VENDEDOR: full CRUD on products
        if rol == Usuario.RolUsuario.VENDEDOR:
            return True

        # CONTADOR: read-only
        if rol == Usuario.RolUsuario.CONTADOR:
            return request.method in SAFE_METHODS

        return False


class InventarioObjectPermission(BasePermission):
    """
    Object-level permission for Inventario module.
    Ensures obj.empresa == request.empresa (tenant defense-in-depth).
    """
    message = "No tiene permiso para acceder a este registro de inventario."

    def has_object_permission(self, request, view, obj):
        empresa = getattr(request, "empresa", None)
        if not empresa:
            return False
        return str(getattr(obj, "empresa_id", None)) == str(empresa.id)
