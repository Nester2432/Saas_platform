"""
modules/ventas/tests/factories.py

Lightweight factory helpers for the ventas test suite.

Dependency chain:
    make_empresa()
        └─ make_admin(empresa)
        └─ make_cliente(empresa)
        └─ make_metodo_pago(empresa)
        └─ make_producto_con_stock(empresa, stock)   ← thin wrapper over inventario
        └─ make_venta_borrador(empresa)
            └─ make_linea(empresa, venta, producto, precio, cantidad)
        └─ setup_venta_completa(...)                 ← full confirmed sale ready to test

Core rule: every factory that needs stock calls MovimientoService — never sets
Producto.stock_actual directly — so Invariant I3 is always satisfied in tests.
"""

import uuid
from decimal import Decimal
from datetime import date

from apps.empresas.models import Empresa, EmpresaConfiguracion
from apps.modulos.models import Modulo, EmpresaModulo
from apps.usuarios.models import Usuario

from modules.clientes.models import Cliente
from modules.inventario.models import Producto
from modules.inventario.services import MovimientoService
from modules.ventas.models import (
    EstadoVenta,
    LineaVenta,
    MetodoPago,
    PagoVenta,
    TipoMetodoPago,
    Venta,
)
from modules.ventas.services import VentaService


# ─────────────────────────────────────────────────────────────────────────────
# Core platform factories (mirrors inventario/tests/factories.py)
# ─────────────────────────────────────────────────────────────────────────────

from modules.billing.models import Plan, Suscripcion, EstadoSuscripcion
from django.utils import timezone

def make_empresa(**kwargs) -> Empresa:
    uid = uuid.uuid4().hex[:8]
    defaults = {
        "nombre":    f"Empresa Test {uid}",
        "slug":      f"empresa-{uid}",
        "email":     f"admin@empresa-{uid}.com",
        "plan":      Empresa.Plan.PROFESSIONAL,
        "is_active": True,
    }
    defaults.update(kwargs)
    empresa = Empresa.objects.create(**defaults)
    EmpresaConfiguracion.objects.get_or_create(empresa=empresa)

    plan, _ = Plan.objects.get_or_create(
        slug="test-plan",
        defaults={
            "nombre": "Test Plan",
            "precio_mensual": 0,
            "activo": True
        }
    )
    Suscripcion.objects.create(
        empresa=empresa,
        plan=plan,
        estado=EstadoSuscripcion.ACTIVA,
        fecha_inicio=timezone.now().date()
    )

    return empresa


def make_admin(empresa: Empresa, **kwargs) -> Usuario:
    uid = uuid.uuid4().hex[:8]
    defaults = {
        "nombre":           "Admin",
        "apellido":         "Test",
        "is_active":        True,
        "is_empresa_admin": True,
    }
    defaults.update(kwargs)
    email    = defaults.pop("email",    f"admin-{uid}@test.com")
    password = defaults.pop("password", "testpass123")
    return Usuario.objects.create_user(
        email=email, empresa=empresa, password=password, **defaults
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ventas domain factories
# ─────────────────────────────────────────────────────────────────────────────

def make_cliente(empresa: Empresa, **kwargs) -> Cliente:
    uid = uuid.uuid4().hex[:6]
    defaults = {
        "nombre":   "Cliente",
        "apellido": f"Test {uid}",
        "email":    f"cliente-{uid}@test.com",
        "telefono": "+5491155550000",
        "activo":   True,
    }
    defaults.update(kwargs)
    return Cliente.objects.create(empresa=empresa, **defaults)


def make_metodo_pago(empresa: Empresa, **kwargs) -> MetodoPago:
    uid = uuid.uuid4().hex[:6]
    defaults = {
        "nombre":        f"Efectivo {uid}",
        "tipo":          TipoMetodoPago.EFECTIVO,
        "activo":        True,
        "acepta_vuelto": True,
        "orden":         0,
    }
    defaults.update(kwargs)
    return MetodoPago.objects.create(empresa=empresa, **defaults)


def make_producto_con_stock(
    empresa: Empresa,
    stock: int = 20,
    precio_venta: Decimal = Decimal("100.00"),
    precio_costo: Decimal = Decimal("60.00"),
    **kwargs,
) -> Producto:
    """
    Create a Producto and load initial stock via MovimientoService.

    Uses the service (not direct field assignment) so Invariant I3 holds
    from the start. Tests built on this factory are always consistent.
    """
    uid = uuid.uuid4().hex[:6]
    producto = Producto.objects.create(
        empresa      = empresa,
        nombre       = kwargs.pop("nombre", f"Producto {uid}"),
        codigo       = kwargs.pop("codigo", f"SKU-{uid}"),
        precio_venta = precio_venta,
        precio_costo = precio_costo,
        stock_actual = 0,
        stock_minimo = 2,
        activo       = True,
        **kwargs,
    )
    if stock > 0:
        MovimientoService.registrar_entrada(
            empresa         = empresa,
            producto        = producto,
            cantidad        = stock,
            motivo          = "Stock inicial de prueba",
            referencia_tipo = "stock_inicial",
        )
        producto.refresh_from_db()
    return producto


def make_venta_borrador(
    empresa: Empresa,
    cliente=None,
    usuario=None,
    **kwargs,
) -> Venta:
    """Create a Venta in BORRADOR state with no lines."""
    return VentaService.crear_venta(
        empresa  = empresa,
        cliente  = cliente,
        usuario  = usuario,
        **kwargs,
    )


def make_linea(
    empresa: Empresa,
    venta: Venta,
    producto: Producto = None,
    descripcion: str = "",
    precio_unitario: Decimal = None,
    cantidad: int = 1,
    descuento: Decimal = Decimal("0"),
    usuario=None,
) -> LineaVenta:
    """Add one line to a BORRADOR venta via VentaService."""
    return VentaService.agregar_linea(
        empresa         = empresa,
        venta           = venta,
        producto        = producto,
        descripcion     = descripcion,
        precio_unitario = precio_unitario,
        cantidad        = cantidad,
        descuento       = descuento,
        usuario         = usuario,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Compound setup helpers
# ─────────────────────────────────────────────────────────────────────────────

def setup_contexto_base() -> dict:
    """
    Create the minimum shared context for ventas tests:
        empresa, admin, cliente, metodo_pago, producto (stock=20)

    Returns a dict with all entities. Used in setUp() to avoid repetition.
    """
    empresa      = make_empresa()
    admin        = make_admin(empresa)
    cliente      = make_cliente(empresa)
    metodo_pago  = make_metodo_pago(empresa)
    producto     = make_producto_con_stock(empresa, stock=20)
    return {
        "empresa":     empresa,
        "admin":       admin,
        "cliente":     cliente,
        "metodo_pago": metodo_pago,
        "producto":    producto,
    }


def setup_venta_confirmada(
    empresa: Empresa,
    producto: Producto,
    metodo_pago: MetodoPago,
    cantidad: int = 5,
    precio: Decimal = Decimal("100.00"),
    usuario=None,
) -> dict:
    """
    Build and confirm a sale for `cantidad` units of `producto`.

    Returns: {"venta": Venta(CONFIRMADA or PAGADA), "linea": LineaVenta}

    Used in tests for cancellation, return, and payment flows that need a
    pre-confirmed sale to operate on.
    """
    venta = make_venta_borrador(empresa, usuario=usuario)
    linea = make_linea(
        empresa         = empresa,
        venta           = venta,
        producto        = producto,
        precio_unitario = precio,
        cantidad        = cantidad,
        usuario         = usuario,
    )
    venta = VentaService.confirmar_venta(
        empresa = empresa,
        venta   = venta,
        pagos   = [{
            "metodo_pago": metodo_pago,
            "monto":       precio * cantidad,
        }],
        usuario = usuario,
    )
    venta.refresh_from_db()
    linea.refresh_from_db()
    return {"venta": venta, "linea": linea}