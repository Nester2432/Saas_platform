from rest_framework.permissions import BasePermission
from core.permissions.base import IsTenantAuthenticated
from apps.usuarios.models import Usuario

class IsAdmin(IsTenantAuthenticated):
    """
    Requires the user to have the 'ADMIN' role within their empresa.
    """
    message = "Requiere rol de Administrador."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return request.user.rol == Usuario.RolUsuario.ADMIN


class IsVendedor(IsTenantAuthenticated):
    """
    Requires the user to have at least 'VENDEDOR' role (or higher).
    """
    message = "Requiere rol de Vendedor o superior."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return request.user.rol in [Usuario.RolUsuario.ADMIN, Usuario.RolUsuario.VENDEDOR]


class IsContador(IsTenantAuthenticated):
    """
    Requires the user to have at least 'CONTADOR' role (or higher).
    """
    message = "Requiere rol de Contador o superior."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return request.user.rol in [Usuario.RolUsuario.ADMIN, Usuario.RolUsuario.CONTADOR]
