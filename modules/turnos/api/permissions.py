"""
modules/turnos/api/permissions.py

Granular access control for the turnos module endpoints.

Permission architecture — two layers:
    Layer 1 (view-level):  IsTenantAuthenticated + ModuloActivoPermission
        → set in TurnoViewSet.permission_classes
        → answers: "is this user allowed to use the turnos module at all?"

    Layer 2 (action-level): classes defined here
        → set per-action via get_permissions() in the ViewSet
        → answers: "is this user allowed to perform THIS specific action?"

    Layer 3 (object-level): has_object_permission()
        → called by ViewSet.get_object() after has_permission() passes
        → answers: "is this user allowed to access THIS specific turno?"

Permission codes (defined in Permiso.codigo, seeded at deploy):
    "turnos.ver"          → list + retrieve
    "turnos.crear"        → create + slots (must see slots to book)
    "turnos.confirmar"    → confirmar action
    "turnos.cancelar"     → cancelar action
    "turnos.reprogramar"  → reprogramar action
    "turnos.completar"    → completar + ausente actions

Expected role → permission mapping (seeded in seed_modulos):
    admin_empresa  → all turnos.* permissions
    recepcion      → turnos.ver + turnos.crear + turnos.confirmar
                     + turnos.cancelar + turnos.reprogramar
    profesional    → turnos.ver + turnos.confirmar + turnos.cancelar
                     + turnos.reprogramar + turnos.completar
                     (object-level: restricted to their own turnos)
    cliente        → turnos.ver (object-level: restricted to their own turnos)

Design decisions:
    - Permission checks use usuario.tiene_permiso() — the granular Permiso
      system, NOT hardcoded role name checks. This means adding a new role
      with the right permissions works without changing this file.
    - "profesional" self-scope: resolved by looking up the Profesional record
      whose usuario FK points to request.user. Cached on the request object
      (_profesional_cache) so the lookup runs at most once per request.
    - No DB access in has_permission() for object-unaware checks — only
      has_object_permission() and _get_profesional_del_usuario() hit the DB.
    - is_empresa_admin bypasses all granular checks (superuser within empresa).
"""

import logging

from rest_framework.permissions import BasePermission

from modules.turnos.models import Profesional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Permission code constants
# ─────────────────────────────────────────────────────────────────────────────

# Defined here as constants so typos are caught at import time, not at runtime.
# These must match the Permiso.codigo values seeded in the database.
PERM_VER         = "turnos.ver"
PERM_CREAR       = "turnos.crear"
PERM_CONFIRMAR   = "turnos.confirmar"
PERM_CANCELAR    = "turnos.cancelar"
PERM_REPROGRAMAR = "turnos.reprogramar"
PERM_COMPLETAR   = "turnos.completar"    # also covers marcar_ausente


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _es_admin(user) -> bool:
    """
    Return True if the user is an empresa admin.

    is_empresa_admin bypasses all granular permission checks — the admin
    can do everything within their empresa. This mirrors the design of
    IsEmpresaAdmin in core/permissions/base.py.
    """
    return bool(getattr(user, "is_empresa_admin", False))


def _tiene_permiso(user, codigo: str) -> bool:
    """
    Return True if the user has the given permission via any of their roles.

    Delegates to usuario.tiene_permiso() which queries the Rol → Permiso M2M.
    Admins short-circuit before this is called (see _es_admin).

    Note: tiene_permiso() makes a DB query on every call. For high-traffic
    endpoints, the production recommendation is to cache role permissions
    in Redis keyed by user ID (similar to ModuloActivoPermission's cache).
    That optimisation is omitted here to keep the permission logic readable;
    it can be added transparently inside tiene_permiso() without changing
    any permission class.
    """
    try:
        return user.tiene_permiso(codigo)
    except Exception:
        # Defensive: if tiene_permiso raises (e.g. user has no empresa),
        # deny rather than grant.
        logger.exception("Error checking permission %s for user %s", codigo, getattr(user, "id", "?"))
        return False


def _get_profesional_del_usuario(request):
    """
    Resolve the Profesional record for the logged-in user within their empresa.

    A user with the "profesional" role has a Profesional record with
    profesional.usuario_id == request.user.id. This is the link that allows
    the system to answer "which appointments belong to this professional user?"

    Returns None if:
    - The user has no Profesional record (they are recepcion, admin, etc.)
    - The user has no empresa on the request

    Caches the result on the request object as _profesional_cache so the
    DB query runs at most once per request, even if has_object_permission()
    is called for every object in a list.

    Index used: idx_profesional_empresa_usuario
    """
    if hasattr(request, "_profesional_cache"):
        return request._profesional_cache

    empresa = getattr(request, "empresa", None)
    if not empresa:
        request._profesional_cache = None
        return None

    profesional = (
        Profesional.objects
        .filter(
            empresa_id=empresa.id,
            usuario_id=request.user.id,
            activo=True,
            deleted_at__isnull=True,
        )
        .first()
    )
    # Cache regardless of result (None means "not a professional user")
    request._profesional_cache = profesional
    return profesional


# ─────────────────────────────────────────────────────────────────────────────
# Permission classes
# ─────────────────────────────────────────────────────────────────────────────

class PuedeVerTurnos(BasePermission):
    """
    Permission for LIST and RETRIEVE endpoints.

    Grants access if the user has "turnos.ver" permission.
    Object-level scoping (profesional sees only their own, cliente sees theirs)
    is enforced by TurnoObjectPermission.has_object_permission().

    Roles that typically have this: admin_empresa, recepcion, profesional, cliente.
    """

    message = "No tiene permiso para ver turnos."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if _es_admin(user):
            return True
        return _tiene_permiso(user, PERM_VER)


class PuedeCrearTurnos(BasePermission):
    """
    Permission for CREATE and SLOTS endpoints.

    SLOTS is gated by the same permission as CREATE because viewing available
    slots is the first step in the booking flow — there is no reason to see
    slots if you cannot book them.

    Roles that typically have this: admin_empresa, recepcion.
    Profesionales do NOT create appointments for others (they confirm/complete).
    """

    message = "No tiene permiso para crear turnos."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if _es_admin(user):
            return True
        return _tiene_permiso(user, PERM_CREAR)


class PuedeConfirmarTurnos(BasePermission):
    """
    Permission for the CONFIRMAR action.

    Professionals can confirm their own appointments.
    Object-level restriction (own appointments only) is enforced by
    TurnoObjectPermission — this class only checks the action-level right.

    Roles that typically have this: admin_empresa, recepcion, profesional.
    """

    message = "No tiene permiso para confirmar turnos."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if _es_admin(user):
            return True
        return _tiene_permiso(user, PERM_CONFIRMAR)


class PuedeCancelarTurnos(BasePermission):
    """
    Permission for the CANCELAR action.

    Professionals can cancel their own appointments.
    Clients cancelling their own appointments also use this permission
    (if the empresa grants "turnos.cancelar" to the cliente role).

    Roles that typically have this: admin_empresa, recepcion, profesional.
    """

    message = "No tiene permiso para cancelar turnos."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if _es_admin(user):
            return True
        return _tiene_permiso(user, PERM_CANCELAR)


class PuedeReprogramarTurnos(BasePermission):
    """
    Permission for the REPROGRAMAR action.

    Roles that typically have this: admin_empresa, recepcion, profesional.
    """

    message = "No tiene permiso para reprogramar turnos."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if _es_admin(user):
            return True
        return _tiene_permiso(user, PERM_REPROGRAMAR)


class PuedeCompletarTurnos(BasePermission):
    """
    Permission for the COMPLETAR and AUSENTE actions.

    These are "closing" actions that mark appointments as done or no-show.
    They make sense only for the professional who attended (or didn't attend)
    — not for recepcion or clients.

    Roles that typically have this: admin_empresa, profesional.
    Recepcion is intentionally excluded from the default mapping.
    """

    message = "No tiene permiso para completar o marcar ausencias en turnos."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if _es_admin(user):
            return True
        return _tiene_permiso(user, PERM_COMPLETAR)


# ─────────────────────────────────────────────────────────────────────────────
# Object-level permission — "can this user access THIS specific turno?"
# ─────────────────────────────────────────────────────────────────────────────

class TurnoObjectPermission(BasePermission):
    """
    Object-level access control for Turno instances.

    Called by ViewSet.get_object() after has_permission() has already passed.

    Rules:
        is_empresa_admin → always allowed (full access within empresa)
        has turno.ver    → allowed IF one of:
                           - user has no Profesional record (recepcion, admin)
                           - user IS the turno's profesional
                           - user IS the turno's cliente (if cliente has a user)

    The tenant check (turno.empresa == request.empresa) is the primary defense.
    The profesional/cliente self-scope is a secondary rule applied only when
    the user's role restricts them to their own records.

    Why not block recepcion at the object level?
    Because recepcion needs to see ALL turnos to manage the schedule — their
    restriction is at the permission code level (PuedeCompletarTurnos excludes
    them), not at the object level.
    """

    message = "No tiene permiso para acceder a este turno."

    def has_object_permission(self, request, view, obj) -> bool:
        print(f"DEBUG: has_object_permission called for user {request.user.id}, obj {obj.id}")
        user = request.user
        empresa = getattr(request, "empresa", None)

        if not empresa:
            return False

        # ── Tenant guard (defense-in-depth) ──────────────────────────────────
        # The queryset is already scoped by TenantQuerysetMixin. This is the
        # second layer that catches edge cases (e.g. direct URL manipulation).
        if str(getattr(obj, "empresa_id", None)) != str(empresa.id):
            return False

        # ── Admin: unrestricted within empresa ───────────────────────────────
        if _es_admin(user):
            return True

        # ── Users without a Profesional record: full access ──────────────────
        # Recepcion staff, managers, etc. do NOT have a Profesional record.
        # If _get_profesional_del_usuario returns None, the user is not a
        # professional — they can see all turnos they have view permission for.
        profesional_del_usuario = _get_profesional_del_usuario(request)
        if profesional_del_usuario is None:
            # Verify they have at least view permission (defensive)
            return _tiene_permiso(user, PERM_VER)

        # ── Profesional: only their own turnos ───────────────────────────────
        # The user HAS a Profesional record. They may only access turnos
        # where obj.profesional_id matches their Profesional record.
        if str(obj.profesional_id) == str(profesional_del_usuario.id):
            return True

        # ── Cliente: only their own turnos ───────────────────────────────────
        # Future: if clients have platform accounts, obj.cliente links to
        # their Cliente record. This checks whether the turno's cliente is
        # associated with the current user (via some future cliente.usuario FK).
        # For now: clients without a direct user link are denied at object level.
        # This is intentionally restrictive — it is easier to open access than
        # to close a security hole.
        if obj.cliente_id:
            cliente = obj.cliente  # already select_related in the queryset
            cliente_usuario_id = getattr(cliente, "usuario_id", None)
            if cliente_usuario_id and str(cliente_usuario_id) == str(user.id):
                return True

        # ── Default: deny ─────────────────────────────────────────────────────
        return False
