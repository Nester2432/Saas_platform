from django.db import models
from core.models import EmpresaModel
from django.core.exceptions import ValidationError
from .catalogo import Producto

class EstadoOrdenCompra(models.TextChoices):
    """
    Lifecycle states for a purchase order.

    Valid transitions (enforced in OrdenCompraService):

        BORRADOR ──enviar──► ENVIADA ──recibir parcial──► RECIBIDA_PARCIAL
                                  │                              │
                                  │                    ──recibir completo──►RECIBIDA_COMPLETA
                                  │
                                  └──cancelar──► CANCELADA

        BORRADOR ──cancelar──► CANCELADA

    Terminal states (no further transitions):
        RECIBIDA_COMPLETA, CANCELADA

    Note: RECIBIDA_PARCIAL is not terminal — further recepciones are allowed
    until the order is fully received. Each recepcion call invokes
    MovimientoService.registrar_entrada() for the newly received quantities.
    """
    BORRADOR           = "BORRADOR",           "Borrador"
    ENVIADA            = "ENVIADA",            "Enviada al proveedor"
    RECIBIDA_PARCIAL   = "RECIBIDA_PARCIAL",   "Recibida parcialmente"
    RECIBIDA_COMPLETA  = "RECIBIDA_COMPLETA",  "Recibida completamente"
    CANCELADA          = "CANCELADA",          "Cancelada"


class Proveedor(EmpresaModel):
    """
    A vendor from whom the empresa purchases stock.

    Linked to OrdenCompra; not directly linked to MovimientoStock.
    The chain is: Proveedor → OrdenCompra → recibir_orden() → MovimientoStock(ENTRADA).

    cuit is the Argentine tax ID (CUIT/CUIL). Stored as a plain string to
    accommodate different formats (with/without hyphens) and future
    internationalization without a schema migration.
    """

    nombre    = models.CharField(max_length=200)
    cuit      = models.CharField(
        max_length=20,
        blank=True,
        help_text="CUIT/CUIL or equivalent tax identifier.",
    )
    email     = models.EmailField(blank=True)
    telefono  = models.CharField(max_length=30, blank=True)
    direccion = models.TextField(blank=True)
    activo    = models.BooleanField(
        default=True,
        help_text="Inactive vendors do not appear in the new-order flow.",
    )
    notas     = models.TextField(blank=True)

    class Meta:
        db_table            = "inventario_proveedor"
        verbose_name        = "Proveedor"
        verbose_name_plural = "Proveedores"
        ordering            = ["nombre"]
        indexes = [
            # "List active vendors for this empresa"
            models.Index(
                fields=["empresa", "activo", "nombre"],
                name="idx_proveedor_empresa_activo",
            ),
        ]

    def __str__(self):
        return self.nombre


class OrdenCompra(EmpresaModel):
    """
    A purchase order sent to a Proveedor.

    State machine:
        BORRADOR → ENVIADA → RECIBIDA_PARCIAL → RECIBIDA_COMPLETA
        BORRADOR → CANCELADA
        ENVIADA  → CANCELADA

    Stock is only affected when recibir_orden() is called (transition to
    RECIBIDA_PARCIAL or RECIBIDA_COMPLETA). Creating or sending an order
    does NOT change stock.

    Partial reception:
        An order for 100 units may be received in multiple steps:
        Step 1: recibir 60 → estado = RECIBIDA_PARCIAL, creates ENTRADA(60)
        Step 2: recibir 40 → estado = RECIBIDA_COMPLETA, creates ENTRADA(40)
        Each step is atomic: all movements in a step commit together or not at all.

    numero is a free-text reference number (the empresa's own PO number).
    It is optional — some businesses don't maintain PO numbers.
    """

    proveedor = models.ForeignKey(
        Proveedor,
        on_delete=models.PROTECT,
        related_name="ordenes",
        help_text="Vendor receiving this purchase order.",
    )
    estado = models.CharField(
        max_length=30,
        choices=EstadoOrdenCompra.choices,
        default=EstadoOrdenCompra.BORRADOR,
    )
    numero = models.CharField(
        max_length=50,
        blank=True,
        help_text="Optional internal PO number for reference.",
    )
    fecha_emision = models.DateField(
        help_text="Date the order was created.",
    )
    fecha_esperada = models.DateField(
        null=True,
        blank=True,
        help_text="Expected delivery date.",
    )
    fecha_recepcion = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Actual first reception timestamp (set on first recibir_orden call).",
    )
    notas = models.TextField(blank=True)

    class Meta:
        db_table            = "inventario_orden_compra"
        verbose_name        = "Orden de Compra"
        verbose_name_plural = "Órdenes de Compra"
        ordering            = ["-fecha_emision"]
        constraints = [
            # fecha_esperada, when set, must be on or after fecha_emision.
            models.CheckConstraint(
                check=(
                    models.Q(fecha_esperada__isnull=True)
                    | models.Q(fecha_esperada__gte=models.F("fecha_emision"))
                ),
                name="check_orden_fecha_esperada_gte_emision",
            ),
        ]
        indexes = [
            # "All open orders for this empresa" (purchasing dashboard)
            models.Index(
                fields=["empresa", "estado", "-fecha_emision"],
                name="idx_orden_empresa_estado",
            ),
            # "All orders for this vendor"
            models.Index(
                fields=["empresa", "proveedor", "-fecha_emision"],
                name="idx_orden_empresa_proveedor",
            ),
        ]

    @property
    def es_editable(self) -> bool:
        """Only BORRADOR orders can be edited (items added/removed)."""
        return self.estado == EstadoOrdenCompra.BORRADOR

    @property
    def es_terminal(self) -> bool:
        """RECIBIDA_COMPLETA and CANCELADA accept no further transitions."""
        return self.estado in (
            EstadoOrdenCompra.RECIBIDA_COMPLETA,
            EstadoOrdenCompra.CANCELADA,
        )

    @property
    def esta_recibida_completamente(self) -> bool:
        """True when every line item has been fully received."""
        return all(
            d.cantidad_recibida >= d.cantidad_pedida
            for d in self.detalles.all()
        )

    def __str__(self):
        num = f" #{self.numero}" if self.numero else ""
        return f"OC{num} — {self.proveedor} ({self.get_estado_display()})"


class OrdenCompraDetalle(EmpresaModel):
    """
    A single line item in a purchase order.

    Tracks both the quantity ordered and the quantity received, enabling
    partial reception tracking across multiple OrdenCompraService.recibir_orden()
    calls.

    Invariant: cantidad_recibida <= cantidad_pedida
    (Enforced by CheckConstraint; recibir_orden() also validates before accepting.)

    precio_unitario is the agreed per-unit cost with the vendor. When a
    reception creates a MovimientoStock(ENTRADA), this value is passed as
    costo_unitario for inventory valuation purposes.
    """

    orden = models.ForeignKey(
        OrdenCompra,
        on_delete=models.CASCADE,
        related_name="detalles",
    )
    producto = models.ForeignKey(
        Producto,
        on_delete=models.PROTECT,
        related_name="detalles_orden",
    )
    cantidad_pedida = models.PositiveIntegerField(
        help_text="Units ordered from the vendor.",
    )
    cantidad_recibida = models.PositiveIntegerField(
        default=0,
        help_text="Units received so far. Updated by recibir_orden().",
    )
    precio_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Agreed cost per unit. Passed to MovimientoStock.costo_unitario on reception.",
    )

    class Meta:
        db_table            = "inventario_orden_compra_detalle"
        verbose_name        = "Detalle de Orden de Compra"
        verbose_name_plural = "Detalles de Órdenes de Compra"
        constraints = [
            # cantidad_pedida must be strictly positive.
            models.CheckConstraint(
                check=models.Q(cantidad_pedida__gt=0),
                name="check_detalle_cantidad_pedida_positiva",
            ),
            # Cannot receive more than was ordered.
            models.CheckConstraint(
                check=models.Q(cantidad_recibida__lte=models.F("cantidad_pedida")),
                name="check_detalle_recibida_lte_pedida",
            ),
            # The same product must not appear twice in a single order.
            models.UniqueConstraint(
                fields=["orden", "producto"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_detalle_producto_por_orden",
            ),
        ]
        indexes = [
            # "All line items in this order" (order detail view)
            models.Index(
                fields=["orden", "producto"],
                name="idx_detalle_orden_producto",
            ),
            # "All orders containing this product" (product purchase history)
            models.Index(
                fields=["empresa", "producto"],
                name="idx_detalle_empresa_producto",
            ),
        ]

    @property
    def pendiente_recepcion(self) -> int:
        """Units ordered but not yet received."""
        return self.cantidad_pedida - self.cantidad_recibida

    @property
    def esta_completo(self) -> bool:
        """True when all ordered units have been received."""
        return self.cantidad_recibida >= self.cantidad_pedida

    def __str__(self):
        return (
            f"{self.producto} × {self.cantidad_pedida} "
            f"(recibido: {self.cantidad_recibida})"
        )
