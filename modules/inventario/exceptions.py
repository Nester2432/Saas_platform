"""
modules/inventario/exceptions.py

Domain exceptions for the inventario module.

All exceptions inherit from Django's ValidationError so the platform's
custom_exception_handler (core/exceptions.py) converts them automatically
to structured HTTP responses — no extra handling needed in views.

HTTP status mapping (handled in views._handle_service_error):
    StockInsuficienteError      → 409 Conflict
    ProductoInactivoError       → 409 Conflict
    AjusteInnecesarioError      → 400 Bad Request
    TransicionOrdenInvalidaError → 409 Conflict
    RecepcionExcedeLoPedidoError → 400 Bad Request

Structured response shape (example):
    {
        "error": true,
        "code": "STOCK_INSUFICIENTE",
        "message": "Stock insuficiente para el producto 'Café molido 250g'.",
        "details": {
            "disponible": 3,
            "solicitado": 8,
            "producto_id": "uuid..."
        }
    }
"""

from django.core.exceptions import ValidationError


class StockInsuficienteError(ValidationError):
    """
    Raised when a stock reduction is attempted but the available stock
    is less than the requested quantity, and the product does not allow
    negative stock (permite_stock_negativo = False).

    Carries structured context so the caller can display:
        "Solo hay 3 unidades disponibles, se solicitaron 8."

    Args:
        producto:    The Producto instance (or name string) being sold.
        disponible:  Current stock_actual at the moment of the check.
        solicitado:  Quantity that was requested.
    """

    error_code = "STOCK_INSUFICIENTE"

    def __init__(self, producto, disponible: int, solicitado: int):
        self.producto   = producto
        self.disponible = disponible
        self.solicitado = solicitado

        nombre = getattr(producto, "nombre", str(producto))
        mensaje = (
            f"Stock insuficiente para el producto '{nombre}'. "
            f"Disponible: {disponible}, solicitado: {solicitado}."
        )
        super().__init__(mensaje, code=self.error_code)

    def __str__(self):
        return (
            f"StockInsuficienteError("
            f"disponible={self.disponible}, solicitado={self.solicitado})"
        )


class ProductoInactivoError(ValidationError):
    """
    Raised when a stock operation is attempted on an inactive product.

    An inactive product (activo=False) must not participate in any stock
    movement — it has been logically retired from operations.

    Args:
        producto: The inactive Producto instance (or name string).
    """

    error_code = "PRODUCTO_INACTIVO"

    def __init__(self, producto):
        self.producto = producto
        nombre = getattr(producto, "nombre", str(producto))
        mensaje = (
            f"El producto '{nombre}' está inactivo y no puede "
            f"recibir movimientos de stock."
        )
        super().__init__(mensaje, code=self.error_code)

    def __str__(self):
        nombre = getattr(self.producto, "nombre", str(self.producto))
        return f"ProductoInactivoError(producto='{nombre}')"


class AjusteInnecesarioError(ValidationError):
    """
    Raised when ajustar_stock() is called with a target value that equals
    the current stock_actual — no movement would be created.

    This is a caller error: if the physical count confirms the recorded
    stock, the result should be logged as an audit event, not sent to
    MovimientoService.ajustar_stock().

    Args:
        producto:     The Producto instance (or name string).
        stock_actual: The current stock value (equals the requested target).
    """

    error_code = "AJUSTE_INNECESARIO"

    def __init__(self, producto, stock_actual: int):
        self.producto     = producto
        self.stock_actual = stock_actual

        nombre = getattr(producto, "nombre", str(producto))
        mensaje = (
            f"El stock actual de '{nombre}' ya es {stock_actual}. "
            f"No se requiere ajuste."
        )
        super().__init__(mensaje, code=self.error_code)

    def __str__(self):
        return f"AjusteInnecesarioError(stock_actual={self.stock_actual})"


class TransicionOrdenInvalidaError(ValidationError):
    """
    Raised when an OrdenCompra state transition is not permitted.

    Examples:
        BORRADOR → RECIBIDA_COMPLETA (must go through ENVIADA first)
        RECIBIDA_COMPLETA → CANCELADA (terminal state)
        CANCELADA → ENVIADA (terminal state)

    Args:
        estado_actual:  The order's current estado value.
        estado_destino: The attempted transition target.
    """

    error_code = "TRANSICION_ORDEN_INVALIDA"

    def __init__(self, estado_actual: str, estado_destino: str):
        self.estado_actual  = estado_actual
        self.estado_destino = estado_destino
        mensaje = (
            f"No se puede cambiar el estado de la orden de "
            f"'{estado_actual}' a '{estado_destino}'."
        )
        super().__init__(mensaje, code=self.error_code)

    def __str__(self):
        return (
            f"TransicionOrdenInvalidaError("
            f"{self.estado_actual} → {self.estado_destino})"
        )


class RecepcionExcedeLoPedidoError(ValidationError):
    """
    Raised when recibir_orden() is called with a quantity that would
    make cantidad_recibida exceed cantidad_pedida for a line item.

    The DB also has check_detalle_recibida_lte_pedida as a last-resort
    constraint, but this exception provides a human-readable message
    before the constraint is ever reached.

    Args:
        producto:          The Producto on the over-received line.
        cantidad_pedida:   Original ordered quantity.
        ya_recibida:       Quantity already received before this call.
        cantidad_recibir:  Quantity being received in this call.
    """

    error_code = "RECEPCION_EXCEDE_LO_PEDIDO"

    def __init__(
        self,
        producto,
        cantidad_pedida: int,
        ya_recibida: int,
        cantidad_recibir: int,
    ):
        self.producto         = producto
        self.cantidad_pedida  = cantidad_pedida
        self.ya_recibida      = ya_recibida
        self.cantidad_recibir = cantidad_recibir

        nombre    = getattr(producto, "nombre", str(producto))
        pendiente = cantidad_pedida - ya_recibida
        mensaje   = (
            f"No se pueden recibir {cantidad_recibir} unidades de '{nombre}'. "
            f"Pendiente de recepción: {pendiente} "
            f"(pedido: {cantidad_pedida}, ya recibido: {ya_recibida})."
        )
        super().__init__(mensaje, code=self.error_code)

    def __str__(self):
        return (
            f"RecepcionExcedeLoPedidoError("
            f"pedido={self.cantidad_pedida}, "
            f"ya_recibida={self.ya_recibida}, "
            f"recibir={self.cantidad_recibir})"
        )
