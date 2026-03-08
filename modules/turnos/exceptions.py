"""
modules/turnos/exceptions.py

Domain exceptions for the turnos module.

All exceptions inherit from Django's ValidationError so the platform's
custom_exception_handler (core/exceptions.py) converts them automatically
to structured 400 responses — no extra handling needed in views.

Usage in services:
    raise TurnoNoDisponibleError("FUERA_DE_HORARIO")
    raise TransicionInvalidaError("PENDIENTE", "COMPLETADO")

Usage in views (automatic via DRF exception handler):
    # Returns HTTP 400:
    # {
    #   "error": "TURNO_NO_DISPONIBLE",
    #   "motivo": "FUERA_DE_HORARIO",
    #   "detail": "El profesional no trabaja en ese horario."
    # }
"""

from django.core.exceptions import ValidationError


class TurnoNoDisponibleError(ValidationError):
    """
    Raised when a requested time slot cannot be booked.

    motivo identifies the specific reason — views and API clients can use
    this to display a targeted error message or suggest alternatives.

    Motivo values (mirrors ResultadoDisponibilidad.motivo):
        "FUERA_DE_HORARIO"  → no HorarioDisponible covers the requested range
        "BLOQUEO_ACTIVO"    → a BloqueoHorario overlaps the requested range
        "TURNO_EXISTENTE"   → an active Turno overlaps the requested range
    """

    error_code = "TURNO_NO_DISPONIBLE"

    _mensajes = {
        "FUERA_DE_HORARIO": "El profesional no trabaja en ese horario.",
        "BLOQUEO_ACTIVO": "El profesional tiene un bloqueo en ese horario.",
        "TURNO_EXISTENTE": "El profesional ya tiene un turno en ese horario.",
    }

    def __init__(self, motivo: str, conflicto=None):
        self.motivo = motivo
        self.conflicto = conflicto
        mensaje = self._mensajes.get(motivo, f"Turno no disponible: {motivo}")
        super().__init__(mensaje, code=self.error_code)

    def __str__(self):
        return f"TurnoNoDisponibleError(motivo={self.motivo})"


class TransicionInvalidaError(ValidationError):
    """
    Raised when a state machine transition is not allowed.

    Example: trying to cancel a COMPLETADO turno.

    Carries the actual and intended states so the API response is specific:
        "No se puede cancelar un turno en estado COMPLETADO."
    """

    error_code = "TRANSICION_INVALIDA"

    def __init__(self, estado_actual: str, estado_destino: str):
        self.estado_actual = estado_actual
        self.estado_destino = estado_destino
        mensaje = (
            f"No se puede cambiar el estado de {estado_actual} a {estado_destino}."
        )
        super().__init__(mensaje, code=self.error_code)

    def __str__(self):
        return (
            f"TransicionInvalidaError("
            f"{self.estado_actual} → {self.estado_destino})"
        )
