"""
modules/inventario/tests/factories.py

Lightweight factory helpers for the inventario test suite.

Each function returns a saved instance with sensible defaults
that can be overridden via keyword arguments.

Dependency chain:
    make_empresa()
        └─ make_usuario(empresa)
        └─ make_admin(empresa)
        └─ make_categoria(empresa)
        └─ make_producto(empresa)      → with optional categoria, stock_actual
        └─ make_proveedor(empresa)
            └─ make_orden_compra(empresa, proveedor)
                └─ make_detalle_orden(empresa, orden, producto, cantidad)
        └─ make_movimiento(empresa, producto, tipo, cantidad)  → raw insert (bypasses service)
"""

import uuid
from decimal import Decimal

from apps.empresas.models import Empresa, EmpresaConfiguracion
from apps.modulos.models import Modulo, EmpresaModulo
from apps.usuarios.models import Usuario

from modules.inventario.models import (
    CategoriaProducto,
    EstadoOrdenCompra,
    MovimientoStock,
    OrdenCompra,
    OrdenCompraDetalle,
    Producto,
    Proveedor,
    TipoMovimiento,
)


# ─────────────────────────────────────────────────────────────────────────────
# Core platform factories
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
        nombre="Starter",
        defaults={
            "precio_mensual": 19,
            "activo": True
        }
    )
    # Signal creates a TRIAL subscription. Update it to ACTIVE for the factory's default behavior.
    Suscripcion.objects.filter(empresa=empresa, estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]).update(
        plan=plan,
        estado=EstadoSuscripcion.ACTIVE,
        fecha_inicio=timezone.now().date()
    )
    
    # In case the signal didn't run or failed
    if not Suscripcion.objects.filter(empresa=empresa).exists():
        Suscripcion.objects.create(
            empresa=empresa,
            plan=plan,
            estado=EstadoSuscripcion.ACTIVE,
            fecha_inicio=timezone.now().date()
        )

    return empresa


def make_usuario(empresa: Empresa, **kwargs) -> Usuario:
    uid = uuid.uuid4().hex[:8]
    defaults = {
        "nombre":           "Test",
        "apellido":         "Usuario",
        "is_active":        True,
        "is_empresa_admin": False,
    }
    defaults.update(kwargs)
    email    = defaults.pop("email",    f"usuario-{uid}@test.com")
    password = defaults.pop("password", "testpass123")
    return Usuario.objects.create_user(
        email=email, empresa=empresa, password=password, **defaults
    )


def make_admin(empresa: Empresa, **kwargs) -> Usuario:
    return make_usuario(empresa=empresa, is_empresa_admin=True, **kwargs)


def activar_modulo(empresa: Empresa, codigo: str) -> EmpresaModulo:
    modulo, _ = Modulo.objects.get_or_create(
        codigo=codigo,
        defaults={"nombre": codigo.capitalize(), "plan_minimo": "free"},
    )
    em, _ = EmpresaModulo.objects.get_or_create(
        empresa=empresa, modulo=modulo, defaults={"activo": True}
    )
    if not em.activo:
        em.activo = True
        em.save(update_fields=["activo"])
    return em


# ─────────────────────────────────────────────────────────────────────────────
# Inventario domain factories
# ─────────────────────────────────────────────────────────────────────────────

def make_categoria(empresa: Empresa, **kwargs) -> CategoriaProducto:
    uid = uuid.uuid4().hex[:6]
    defaults = {
        "nombre": f"Categoría {uid}",
        "color":  "#3B82F6",
        "orden":  0,
    }
    defaults.update(kwargs)
    return CategoriaProducto.objects.create(empresa=empresa, **defaults)


def make_producto(empresa: Empresa, **kwargs) -> Producto:
    """
    Create a Producto with sensible defaults.

    stock_actual defaults to 0 — use make_producto(stock_actual=10) or call
    MovimientoService.registrar_entrada() to set the initial stock via the
    proper channel.

    Pass permite_stock_negativo=True for products used in tests that need to
    go below zero.
    """
    uid = uuid.uuid4().hex[:6]
    defaults = {
        "nombre":                 f"Producto {uid}",
        "codigo":                 f"SKU-{uid}",
        "descripcion":            "Producto de prueba",
        "precio_costo":           Decimal("100.00"),
        "precio_venta":           Decimal("150.00"),
        "stock_actual":           0,
        "stock_minimo":           5,
        "stock_maximo":           None,
        "unidad_medida":          "unidades",
        "permite_stock_negativo": False,
        "activo":                 True,
    }
    defaults.update(kwargs)
    return Producto.objects.create(empresa=empresa, **defaults)


def make_proveedor(empresa: Empresa, **kwargs) -> Proveedor:
    uid = uuid.uuid4().hex[:6]
    defaults = {
        "nombre":   f"Proveedor {uid}",
        "email":    f"proveedor-{uid}@test.com",
        "telefono": f"+54911{uid[:7]}",
        "activo":   True,
    }
    defaults.update(kwargs)
    return Proveedor.objects.create(empresa=empresa, **defaults)


def make_orden_compra(
    empresa: Empresa,
    proveedor: Proveedor,
    **kwargs,
) -> OrdenCompra:
    """Create an OrdenCompra in BORRADOR state."""
    from datetime import date
    defaults = {
        "estado":        EstadoOrdenCompra.BORRADOR,
        "fecha_emision": date.today(),
        "notas":         "",
    }
    defaults.update(kwargs)
    return OrdenCompra.objects.create(
        empresa=empresa, proveedor=proveedor, **defaults
    )


def make_detalle_orden(
    empresa: Empresa,
    orden: OrdenCompra,
    producto: Producto,
    cantidad_pedida: int = 10,
    **kwargs,
) -> OrdenCompraDetalle:
    """Create an OrdenCompraDetalle line item."""
    defaults = {
        "cantidad_pedida":  cantidad_pedida,
        "cantidad_recibida": 0,
        "precio_unitario":  producto.precio_costo,
    }
    defaults.update(kwargs)
    return OrdenCompraDetalle.objects.create(
        empresa=empresa, orden=orden, producto=producto, **defaults
    )


def make_movimiento(
    empresa: Empresa,
    producto: Producto,
    tipo: str = TipoMovimiento.ENTRADA,
    cantidad: int = 10,
    **kwargs,
) -> MovimientoStock:
    """
    Create a MovimientoStock directly in the DB — bypasses MovimientoService.

    Use ONLY for test setup when you need a movement to exist without going
    through service validation. For testing the service itself, call
    MovimientoService directly.

    Note: this factory does NOT update Producto.stock_actual. It is only
    suitable for tests that verify raw model behavior or invariants.
    Use setup_producto_con_stock() when you need stock_actual to reflect
    the movements.
    """
    stock_anterior = producto.stock_actual
    if tipo in TipoMovimiento.tipos_positivos():
        stock_resultante = stock_anterior + cantidad
    else:
        stock_resultante = stock_anterior - cantidad

    defaults = {
        "tipo":             tipo,
        "cantidad":         cantidad,
        "stock_anterior":   stock_anterior,
        "stock_resultante": stock_resultante,
        "motivo":           "Movimiento de prueba",
    }
    defaults.update(kwargs)
    return MovimientoStock.objects.create(
        empresa=empresa, producto=producto, **defaults
    )


# ─────────────────────────────────────────────────────────────────────────────
# Compound setup helper
# ─────────────────────────────────────────────────────────────────────────────

def setup_producto_con_stock(
    stock_inicial: int = 20,
    empresa: Empresa = None,
    **producto_kwargs,
) -> dict:
    """
    Build the minimum graph needed to test stock operations:
        empresa → admin → producto (with stock set via registrar_entrada)

    Uses MovimientoService to set initial stock — so stock_actual and
    MovimientoStock are always consistent. Tests built on this helper
    automatically satisfy Invariant I3.

    Returns a dict with: empresa, admin, producto, movimiento_inicial
    """
    from modules.inventario.services import MovimientoService

    empresa = empresa or make_empresa()
    admin   = make_admin(empresa)
    producto  = make_producto(empresa, **producto_kwargs)

    movimiento = None
    if stock_inicial > 0:
        movimiento = MovimientoService.registrar_entrada(
            empresa=empresa,
            producto=producto,
            cantidad=stock_inicial,
            motivo="Stock inicial de prueba",
            referencia_tipo="stock_inicial",
            usuario=admin,
        )
        producto.refresh_from_db()

    return {
        "empresa":             empresa,
        "admin":               admin,
        "producto":            producto,
        "movimiento_inicial":  movimiento,
    }
