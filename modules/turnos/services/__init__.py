"""
modules/turnos/services/__init__.py

Public re-exports for the turnos services package.

Views and external callers import from this module only:
    from modules.turnos.services import DisponibilidadService, TurnoService

Internal structure (disponibilidad.py, turnos.py) is an implementation detail.
"""

from modules.turnos.services.disponibilidad import (
    DisponibilidadService,
    ResultadoDisponibilidad,
    SlotDisponible,
    MOTIVO_FUERA_DE_HORARIO,
    MOTIVO_BLOQUEO_ACTIVO,
    MOTIVO_TURNO_EXISTENTE,
)
from modules.turnos.services.turnos import TurnoService

__all__ = [
    # Availability — read-only
    "DisponibilidadService",
    "ResultadoDisponibilidad",
    "SlotDisponible",
    "MOTIVO_FUERA_DE_HORARIO",
    "MOTIVO_BLOQUEO_ACTIVO",
    "MOTIVO_TURNO_EXISTENTE",
    # Mutations
    "TurnoService",
]
