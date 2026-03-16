from django.core.exceptions import ValidationError

class PagosError(ValidationError):
    """Base exception for the pagos module."""
    code = "pagos_error"

class SobrePagoError(PagosError):
    """Raised when a payment confirmation would exceed the sale balance."""
    code = "sobrepago_error"
    
    def __init__(self, saldo_pendiente, monto_pago):
        self.saldo_pendiente = saldo_pendiente
        self.monto_pago = monto_pago
        message = f"El pago ({monto_pago}) excede el saldo pendiente ({saldo_pendiente})."
        super().__init__(message, code=self.code)

class TransicionPagoInvalidaError(PagosError):
    """Raised when an invalid state transition is attempted on a Pago."""
    code = "transicion_pago_invalida"
    
    def __init__(self, estado_actual, estado_destino):
        self.estado_actual = estado_actual
        self.estado_destino = estado_destino
        message = f"No se puede pasar de {estado_actual} a {estado_destino}."
        super().__init__(message, code=self.code)
