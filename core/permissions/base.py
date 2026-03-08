"""
core/permissions/base.py

DRF Permission classes for the SaaS platform.

Available permissions:
- IsTenantAuthenticated: user is authenticated AND belongs to an active empresa
- ModuloActivoPermission: the required module is active for this empresa
- IsEmpresaAdmin: user has admin role within their empresa

Usage in ViewSets:
    class ClienteViewSet(viewsets.ModelViewSet):
        permission_classes = [IsTenantAuthenticated, ModuloActivoPermission]
        modulo_requerido = "clientes"
"""

import logging
from django.core.cache import cache
from rest_framework.permissions import BasePermission, IsAuthenticated

logger = logging.getLogger(__name__)

MODULO_CACHE_TTL = 300  # 5 minutes


class IsTenantAuthenticated(IsAuthenticated):
    """
    Extends IsAuthenticated to also verify:
    1. request.empresa is resolved (TenantMiddleware ran correctly)
    2. The authenticated user belongs to that empresa

    This is the BASE permission that should be on every business endpoint.
    """

    message = "Autenticación requerida o empresa no encontrada."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False

        if not getattr(request, "empresa", None):
            return False

        # Verify the user actually belongs to this empresa
        if hasattr(request.user, "empresa_id"):
            return str(request.user.empresa_id) == str(request.empresa.id)

        return False


class ModuloActivoPermission(BasePermission):
    """
    Verifies that the required module is activated for the current empresa.

    Views declare which module they require:
        class ClienteViewSet(viewsets.ModelViewSet):
            modulo_requerido = "clientes"

    Module activation is cached per empresa to avoid a DB hit on every request.

    If modulo_requerido is not set on the view, this permission passes through.
    """

    message = "Este módulo no está activo para su empresa."

    def has_permission(self, request, view):
        modulo_requerido = getattr(view, "modulo_requerido", None)

        if not modulo_requerido:
            # No module requirement declared — pass through
            return True

        if not getattr(request, "empresa", None):
            return False

        return self._is_modulo_activo(request.empresa.id, modulo_requerido)

    def _is_modulo_activo(self, empresa_id, codigo_modulo):
        """Check module activation with caching."""
        cache_key = f"modulo_activo:{empresa_id}:{codigo_modulo}"
        resultado = cache.get(cache_key)

        if resultado is None:
            try:
                from apps.modulos.models import EmpresaModulo
                resultado = EmpresaModulo.objects.filter(
                    empresa_id=empresa_id,
                    modulo__codigo=codigo_modulo,
                    activo=True,
                ).exists()
            except Exception:
                logger.exception(
                    "Error checking module %s for empresa %s",
                    codigo_modulo, empresa_id
                )
                resultado = False

            cache.set(cache_key, resultado, MODULO_CACHE_TTL)

        return resultado


class IsEmpresaAdmin(IsTenantAuthenticated):
    """
    Requires the user to have the 'admin' role within their empresa.
    Used for company management endpoints.
    """

    message = "Se requieren permisos de administrador."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False

        return getattr(request.user, "is_empresa_admin", False)


class IsReadOnly(BasePermission):
    """
    Allows read-only methods: GET, HEAD, OPTIONS.
    Combine with other permissions for read/write split.

    Example:
        permission_classes = [IsTenantAuthenticated, IsReadOnly]
    """

    def has_permission(self, request, view):
        return request.method in ("GET", "HEAD", "OPTIONS")
