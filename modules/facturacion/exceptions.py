from django.core.exceptions import ValidationError

class FacturacionError(ValidationError):
    """Base exception for the facturacion module."""
    code = "facturacion_error"

class FacturaActivaError(FacturacionError):
    """Raised when trying to generate a second active invoice for a sale."""
    code = "factura_activa_error"

class FacturaEmitidaError(FacturacionError):
    """Raised when trying to modify an already issued invoice."""
    code = "factura_emitida_error"
