"""
modules/turnos/api/__init__.py

Public surface of the turnos API package.

Exports the ViewSet and permission classes so that urls.py and any
integration tests can import them from the package root without knowing
the internal module layout:

    from modules.turnos.api import TurnoViewSet
    from modules.turnos.api import TurnoObjectPermission

Internal modules:
    serializers.py   — input validation and output formatting
    views.py         — endpoint orchestration (ViewSet)
    permissions.py   — access control (has_permission / has_object_permission)
"""

from modules.turnos.api.views import TurnoViewSet
from modules.turnos.api.permissions import (
    TurnoObjectPermission,
    PuedeVerTurnos,
    PuedeCrearTurnos,
    PuedeConfirmarTurnos,
    PuedeCancelarTurnos,
    PuedeReprogramarTurnos,
    PuedeCompletarTurnos,
    PERM_VER,
    PERM_CREAR,
    PERM_CONFIRMAR,
    PERM_CANCELAR,
    PERM_REPROGRAMAR,
    PERM_COMPLETAR,
)

__all__ = [
    # ViewSet
    "TurnoViewSet",
    # Permission classes
    "TurnoObjectPermission",
    "PuedeVerTurnos",
    "PuedeCrearTurnos",
    "PuedeConfirmarTurnos",
    "PuedeCancelarTurnos",
    "PuedeReprogramarTurnos",
    "PuedeCompletarTurnos",
    # Permission code constants (for use in tests and seed scripts)
    "PERM_VER",
    "PERM_CREAR",
    "PERM_CONFIRMAR",
    "PERM_CANCELAR",
    "PERM_REPROGRAMAR",
    "PERM_COMPLETAR",
]
