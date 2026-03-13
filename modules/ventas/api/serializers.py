"""
modules/ventas/api/serializers.py

Serializers for the ventas module REST API.

Architecture contract:
    - Serializers own INPUT VALIDATION and OUTPUT FORMATTING only.
    - Serializers NEVER call services, .save(), or ORM methods.
    - empresa is NEVER accepted from the request body — it is always
      injected from request.empresa by the view.
    - FK resolution (UUID → ORM object) is the view's responsibility,
      not the serializer's.

Read / write split:
    Read serializers   → rich nested representation for GET responses.
    Write serializers  → flat, minimal input validation for POST bodies.

N+1 contract:
    Read serializers assume the view queryset has called:
        .select_related("cliente", "created_by")
        .prefetch_related("lineas__producto", "pagos__metodo_pago", "devoluciones")
    Any FK accessed inside a read serializer method will NOT trigger a lazy
    query when the view honours this contract.

Serializer inventory:
    ── Read ──────────────────────────────────────────────────────────────────
    MetodoPagoSerializer         Nested inside PagoVentaSerializer
    ProductoResumenSerializer    Nested inside LineaVentaSerializer
    ClienteResumenSerializer     Nested inside VentaSerializer (nullable)
    LineaVentaSerializer         Nested inside VentaSerializer
    PagoVentaSerializer          Nested inside VentaSerializer
    DevolucionLineaSerializer    Nested inside DevolucionVentaSerializer
    DevolucionVentaSerializer    Nested inside VentaSerializer
    VentaSerializer              Full GET /ventas/ and GET /ventas/{id}/

    ── Write ─────────────────────────────────────────────────────────────────
    CrearVentaSerializer         POST /ventas/
    AgregarLineaSerializer       POST /ventas/{id}/agregar_linea/
    QuitarLineaSerializer        POST /ventas/{id}/quitar_linea/
    ConfirmarVentaSerializer     POST /ventas/{id}/confirmar/
    CancelarVentaSerializer      POST /ventas/{id}/cancelar/
    PagoInputSerializer          single item inside ConfirmarVentaSerializer.pagos
    RegistrarPagoSerializer      POST /ventas/{id}/pagar/
    DevolucionItemSerializer     single item inside RegistrarDevolucionSerializer.items
    RegistrarDevolucionSerializer POST /ventas/{id}/devolver/
"""

from decimal import Decimal

from rest_framework import serializers

from modules.ventas.models import (
    DevolucionLineaVenta,
    DevolucionVenta,
    EstadoVenta,
    LineaVenta,
    MetodoPago,
    PagoVenta,
    Venta,
)


# ─────────────────────────────────────────────────────────────────────────────
# Nested read serializers
# ─────────────────────────────────────────────────────────────────────────────

class MetodoPagoSerializer(serializers.ModelSerializer):
    """
    Compact MetodoPago for embedding inside PagoVentaSerializer.
    The view's prefetch_related("pagos__metodo_pago") makes this free.
    """
    tipo_display = serializers.CharField(source="get_tipo_display", read_only=True)

    class Meta:
        model  = MetodoPago
        fields = ["id", "nombre", "tipo", "tipo_display"]
        read_only_fields = fields


class ProductoResumenSerializer(serializers.Serializer):
    """
    Compact Producto for embedding inside LineaVentaSerializer.

    Uses plain Serializer (not ModelSerializer) because Producto lives in
    modules.inventario — importing it here would create a cross-module model
    import in the API layer. The fields are simple scalars; explicit is safer.

    The view's prefetch_related("lineas__producto") makes this free.
    """
    id           = serializers.UUIDField(read_only=True)
    nombre       = serializers.CharField(read_only=True)
    codigo       = serializers.CharField(read_only=True)
    precio_venta = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    stock_actual = serializers.IntegerField(read_only=True)


class ClienteResumenSerializer(serializers.Serializer):
    """
    Compact Cliente for embedding inside VentaSerializer.

    Same rationale as ProductoResumenSerializer — plain Serializer to avoid
    cross-module model import. The view's select_related("cliente") makes it free.
    """
    id             = serializers.UUIDField(read_only=True)
    nombre         = serializers.CharField(read_only=True)
    apellido       = serializers.CharField(read_only=True)
    email          = serializers.EmailField(read_only=True)
    telefono       = serializers.CharField(read_only=True)
    nombre_completo = serializers.SerializerMethodField()

    def get_nombre_completo(self, obj) -> str:
        return f"{obj.nombre} {obj.apellido}".strip()


class LineaVentaSerializer(serializers.ModelSerializer):
    """
    Full representation of a LineaVenta for embedding inside VentaSerializer.

    producto is nullable (service lines have no product). When producto is
    set, ProductoResumenSerializer is used. When null, the field returns null.
    """
    producto = ProductoResumenSerializer(read_only=True, allow_null=True)
    bruto    = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        help_text="precio_unitario × cantidad before discount.",
    )

    class Meta:
        model  = LineaVenta
        fields = [
            "id",
            "orden",
            "producto",          # nested (null for service lines)
            "descripcion",       # snapshot
            "precio_unitario",   # snapshot
            "cantidad",
            "descuento",
            "bruto",             # computed property
            "subtotal",
            "movimiento_stock",  # UUID link to MovimientoStock (null for services)
        ]
        read_only_fields = fields


class PagoVentaSerializer(serializers.ModelSerializer):
    """Full representation of a PagoVenta for embedding inside VentaSerializer."""
    metodo_pago = MetodoPagoSerializer(read_only=True)

    class Meta:
        model  = PagoVenta
        fields = ["id", "metodo_pago", "monto", "referencia", "fecha"]
        read_only_fields = fields


class DevolucionLineaSerializer(serializers.ModelSerializer):
    """
    Compact DevolucionLineaVenta for embedding inside DevolucionVentaSerializer.
    Returns enough info for a return receipt.
    """
    descripcion_linea = serializers.CharField(
        source="linea_venta.descripcion", read_only=True,
        help_text="Snapshot description from the original line.",
    )

    class Meta:
        model  = DevolucionLineaVenta
        fields = [
            "id",
            "linea_venta",
            "descripcion_linea",
            "cantidad_devuelta",
            "monto_devuelto",
            "movimiento_stock",
        ]
        read_only_fields = fields


class DevolucionVentaSerializer(serializers.ModelSerializer):
    """Full representation of a DevolucionVenta for embedding inside VentaSerializer."""
    lineas = DevolucionLineaSerializer(many=True, read_only=True)

    class Meta:
        model  = DevolucionVenta
        fields = [
            "id",
            "motivo",
            "total_devuelto",
            "fecha",
            "notas",
            "lineas",
        ]
        read_only_fields = fields


# ─────────────────────────────────────────────────────────────────────────────
# VentaSerializer — primary read representation
# ─────────────────────────────────────────────────────────────────────────────

class VentaSerializer(serializers.ModelSerializer):
    """
    Full read representation of a Venta.

    Used for: GET /ventas/, GET /ventas/{id}/, and as the response body
    of all mutation actions (confirmar, cancelar, pagar, devolver).

    N+1 contract — the view queryset MUST have called:
        .select_related("cliente", "created_by")
        .prefetch_related(
            "lineas__producto",
            "pagos__metodo_pago",
            "devoluciones__lineas__linea_venta",
        )
    Without those prefetches, each nested serializer would issue a query per
    object. With them, the entire tree is loaded in 4 SQL statements.

    empresa is intentionally excluded — it is a tenant-internal field, not
    useful in API responses.

    estado_display, total_pagado, saldo_pendiente are computed fields for
    convenience — the front-end should not need to recalculate them.
    """

    cliente         = ClienteResumenSerializer(read_only=True, allow_null=True)
    estado_display  = serializers.CharField(source="get_estado_display", read_only=True)
    lineas          = LineaVentaSerializer(many=True, read_only=True)
    pagos           = PagoVentaSerializer(many=True, read_only=True)
    devoluciones    = DevolucionVentaSerializer(many=True, read_only=True)
    total_pagado    = serializers.SerializerMethodField()
    saldo_pendiente = serializers.SerializerMethodField()

    class Meta:
        model  = Venta
        fields = [
            # Identity
            "id",
            "numero",
            # Relationships
            "cliente",
            "turno",             # UUID (not nested — turno detail lives in turnos module)
            # State
            "estado",
            "estado_display",
            # Dates
            "fecha",
            # Financials
            "subtotal",
            "descuento_total",
            "total",
            "total_pagado",      # computed: Σ pagos
            "saldo_pendiente",   # computed: total - total_pagado
            "pago_diferido",
            # Snapshot
            "datos_cliente",
            # Notes
            "notas",
            # Collections
            "lineas",
            "pagos",
            "devoluciones",
            # Audit
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_total_pagado(self, obj) -> Decimal:
        """
        Sum of all PagoVenta.monto for this sale.

        Uses the prefetched 'pagos' reverse relation — no extra query.
        Accessing obj.pagos.all() when prefetch_related("pagos") was applied
        reads from the prefetch cache, not the DB.
        """
        return sum(p.monto for p in obj.pagos.all()) or Decimal("0")

    def get_saldo_pendiente(self, obj) -> Decimal:
        """outstanding balance = total - Σ pagos."""
        pagado = self.get_total_pagado(obj)
        return max(obj.total - pagado, Decimal("0"))


# ─────────────────────────────────────────────────────────────────────────────
# Write serializers — input validation only
# ─────────────────────────────────────────────────────────────────────────────

class CrearVentaSerializer(serializers.Serializer):
    """
    Input for POST /ventas/ — creates a BORRADOR sale.

    Validates structure only. The view resolves cliente_id and turno_id to
    ORM objects before calling VentaService.crear_venta().

    empresa is never in the body — injected from request.empresa by the view.
    """
    cliente_id      = serializers.UUIDField(
        required=False, allow_null=True, default=None,
        help_text="UUID of clientes.Cliente. Omit for anonymous sales.",
    )
    turno_id        = serializers.UUIDField(
        required=False, allow_null=True, default=None,
        help_text="UUID of turnos.Turno. Set when a sale originates from an appointment.",
    )
    descuento_total = serializers.DecimalField(
        max_digits=14, decimal_places=2,
        required=False, default=Decimal("0"), min_value=Decimal("0"),
        help_text="Sale-level discount applied after line subtotals.",
    )
    pago_diferido   = serializers.BooleanField(
        required=False, default=False,
        help_text="If True, confirmation does not require full payment upfront.",
    )
    notas           = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=2000,
    )


class AgregarLineaSerializer(serializers.Serializer):
    """
    Input for POST /ventas/{id}/agregar_linea/.

    Either producto_id OR (descripcion + precio_unitario) must be provided.
    Cross-field validation is in validate() below.
    """
    producto_id     = serializers.UUIDField(
        required=False, allow_null=True, default=None,
        help_text="UUID of inventario.Producto. Null for service lines.",
    )
    descripcion     = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=200,
        help_text="Line description. Auto-populated from producto.nombre if producto is set.",
    )
    precio_unitario = serializers.DecimalField(
        max_digits=12, decimal_places=2,
        required=False, allow_null=True, default=None, min_value=Decimal("0"),
        help_text="Unit price. Auto-populated from producto.precio_venta if producto is set.",
    )
    cantidad        = serializers.IntegerField(
        min_value=1, default=1,
        help_text="Units to sell.",
    )
    descuento       = serializers.DecimalField(
        max_digits=12, decimal_places=2,
        required=False, default=Decimal("0"), min_value=Decimal("0"),
        help_text="Line-level discount amount (not percentage).",
    )

    def validate(self, data):
        """
        Cross-field: if producto_id is absent, descripcion and precio_unitario
        must be explicitly provided.

        Note: if producto_id IS provided, descripcion and precio_unitario may
        be absent (the view will auto-populate them from the product). If they
        are provided alongside a product, they override the product defaults —
        this is intentional for manual pricing.
        """
        if not data.get("producto_id"):
            if not data.get("descripcion", "").strip():
                raise serializers.ValidationError(
                    {"descripcion": "Requerida cuando no se especifica un producto."}
                )
            if data.get("precio_unitario") is None:
                raise serializers.ValidationError(
                    {"precio_unitario": "Requerido cuando no se especifica un producto."}
                )
        return data


class QuitarLineaSerializer(serializers.Serializer):
    """Input for POST /ventas/{id}/quitar_linea/."""
    linea_id = serializers.UUIDField(
        help_text="UUID of the LineaVenta to remove.",
    )


class PagoInputSerializer(serializers.Serializer):
    """
    One payment item inside ConfirmarVentaSerializer.pagos.
    Also reused inside RegistrarPagoSerializer.
    """
    metodo_pago_id = serializers.UUIDField(
        help_text="UUID of ventas.MetodoPago.",
    )
    monto          = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"),
        help_text="Amount paid via this method.",
    )
    referencia     = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=100,
        help_text="Optional transaction reference (approval code, QR token, etc.).",
    )


class ConfirmarVentaSerializer(serializers.Serializer):
    """
    Input for POST /ventas/{id}/confirmar/.

    pagos may be empty only when pago_diferido=True on the Venta.
    That rule is enforced in VentaService (not here — the service owns V6).

    The view resolves each PagoInputSerializer.metodo_pago_id to a MetodoPago
    instance before calling VentaService.confirmar_venta(pagos=[...]).
    """
    pagos = PagoInputSerializer(
        many=True, required=False, default=list,
        help_text="Payment records. May be empty for pago_diferido sales.",
    )


class CancelarVentaSerializer(serializers.Serializer):
    """
    Input for POST /ventas/{id}/cancelar/.

    motivo is optional — cancellations without a reason are permitted but
    a non-empty motivo improves the audit trail. The service appends it to
    Venta.notas. No ChoiceField for who is cancelling (unlike turnos) because
    in the ventas context, cancellations are always by staff — the acting user
    is captured via request.user.
    """
    motivo = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=2000,
        help_text="Optional reason for cancellation. Appended to Venta.notas.",
    )

    def validate_motivo(self, value):
        return value.strip()


class RegistrarPagoSerializer(serializers.Serializer):
    """
    Input for POST /ventas/{id}/pagar/.

    Registers a single payment against a CONFIRMADA (credit) sale.
    """
    metodo_pago_id = serializers.UUIDField()
    monto          = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"),
    )
    referencia     = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=100,
    )


class DevolucionItemSerializer(serializers.Serializer):
    """One item inside RegistrarDevolucionSerializer.items."""
    linea_id  = serializers.UUIDField(
        help_text="UUID of the LineaVenta being returned.",
    )
    cantidad  = serializers.IntegerField(
        min_value=1,
        help_text="Units being returned. Must be <= sold - already returned.",
    )


class RegistrarDevolucionSerializer(serializers.Serializer):
    """
    Input for POST /ventas/{id}/devolver/.

    The view resolves each DevolucionItemSerializer.linea_id to a LineaVenta
    instance before calling VentaService.registrar_devolucion(items=[...]).
    """
    items  = DevolucionItemSerializer(
        many=True, min_length=1,
        help_text="List of lines and quantities being returned.",
    )
    motivo = serializers.CharField(
        max_length=2000,
        help_text="Reason for the return. Required for audit trail.",
    )
    notas  = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=2000,
    )

    def validate_motivo(self, value):
        stripped = value.strip()
        if not stripped:
            raise serializers.ValidationError("El motivo no puede estar vacío.")
        return stripped