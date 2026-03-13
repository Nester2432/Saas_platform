"""
modules/clientes/permissions.py

Granular object-level permissions for the clientes module.

Class-level access (can I use this endpoint at all?) is handled by:
    IsTenantAuthenticated  — authenticated + belongs to an empresa
    ModuloActivoPermission — empresa has "clientes" module active

Object-level access (can I access THIS specific record?) is handled here:
    ClienteObjectPermission — ensures the object belongs to request.empresa

This two-layer approach means:
1. The ViewSet queryset already filters by empresa (via TenantQuerysetMixin)
   so cross-tenant objects never even appear in lists.
2. On direct object access (retrieve/update/destroy), we add an explicit
   object-level check as a defense-in-depth second layer.
"""

import sys
from rest_framework.permissions import BasePermission
from core.permissions.base import IsTenantAuthenticated, ModuloActivoPermission


class ClienteObjectPermission(BasePermission):
    """
    Object-level permission: the object's empresa must match request.empresa.

    Protects against URL-guessing attacks where a user from empresa A
    tries to access /clientes/<id_from_empresa_B>/.

    Relies on TenantQuerysetMixin having already scoped the queryset —
    this is an extra safety net, not the primary defense.
    """

    message = "No tiene permiso para acceder a este cliente."

    def has_object_permission(self, request, view, obj):
        empresa = getattr(request, "empresa", None)
        if not empresa:
            return False
        return str(getattr(obj, "empresa_id", None)) == str(empresa.id)


class PuedeGestionarClientes(IsTenantAuthenticated, ModuloActivoPermission):
    """
    Composite permission for full client management.

    Requires:
    1. Authenticated user
    2. User belongs to an active empresa (IsTenantAuthenticated)
    3. empresa has "clientes" module active (ModuloActivoPermission)

    Views set: modulo_requerido = "clientes"
    """
    pass


class PuedeLeerClientes(BasePermission):
    """
    Read-only access permission. Allows GET, HEAD, OPTIONS.
    Useful for giving "solo_lectura" roles access to client data.
    """

    def has_permission(self, request, view):
        return request.method in ("GET", "HEAD", "OPTIONS")
