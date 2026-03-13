"""
modules/ventas/exceptions.py

Domain exceptions for the ventas module.

All exceptions inherit from Django's ValidationError so the platform's
custom_exception_handler converts them automatically to structured HTTP
responses — no extra handling needed in views.
"""

from django.core.exceptions import ValidationError


class TransicionVentaInvalidaError(ValidationError):
    """
    Raised when a Venta state transition is not permitted.

    Examples:
        Trying to confirm a CANCELADA sale.
        Trying to add a line to a CONFIRMADA sale.
        Trying to register a payment on a PAGADA sale.
    """

    error_code = "TRANSICION_VENTA_INVALIDA"

    def __init__(self, estado_actual: str, estado_destino: str, detalle: str = ""):
        self.estado_actual  = estado_actual
        self.estado_destino = estado_destino
        mensaje = (
            detalle or
            f"No se puede cambiar la venta de '{estado_actual}' a '{estado_destino}'."
        )
        super().__init__(mensaje, code=self.error_code)

    def __str__(self):
        return (
            f"TransicionVentaInvalidaError("
            f"{self.estado_actual} → {self.estado_destino})"
        )


class VentaSinLineasError(ValidationError):
    """
    Raised when confirmar_venta() is called on a sale with no line items.

    A sale with no lines has no commercial meaning and cannot be confirmed.
    """

    error_code = "VENTA_SIN_LINEAS"

    def __init__(self, venta=None):
        self.venta = venta
        numero = getattr(venta, "numero", None) or "(sin número)"
        super().__init__(
            f"La venta {numero} no tiene líneas. "
            f"Agregue al menos un producto o servicio antes de confirmar.",
            code=self.error_code,
        )


class PagoInsuficienteError(ValidationError):
    """
    Raised when confirmar_venta() is called without full payment
    and pago_diferido is False.

    Carries structured context for the API response:
        "Se requieren $8.000. Se registraron $5.000. Falta: $3.000."
    """

    error_code = "PAGO_INSUFICIENTE"

    def __init__(self, total, pagado, faltante):
        self.total    = total
        self.pagado   = pagado
        self.faltante = faltante
        super().__init__(
            f"El pago registrado ({pagado}) no cubre el total de la venta ({total}). "
            f"Falta: {faltante}.",
            code=self.error_code,
        )

    def __str__(self):
        return (
            f"PagoInsuficienteError("
            f"total={self.total}, pagado={self.pagado}, faltante={self.faltante})"
        )


class DevolucionInvalidaError(ValidationError):
    """
    Raised when registrar_devolucion() receives invalid items.

    Examples:
        A line that does not belong to the sale.
        A quantity exceeding what was sold minus already returned.
        A duplicate line in the items list.
    """

    error_code = "DEVOLUCION_INVALIDA"

    def __init__(self, detalle: str):
        self.detalle = detalle
        super().__init__(detalle, code=self.error_code)