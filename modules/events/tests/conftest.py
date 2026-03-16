import pytest
from decimal import Decimal
from django.utils import timezone
from modules.inventario.tests.factories import make_empresa, make_admin, activar_modulo
from modules.billing.models import Plan, Suscripcion

@pytest.fixture
def plan_pro():
    """Create a default Pro plan for tests."""
    plan, _ = Plan.objects.get_or_create(
        nombre="Pro Plan",
        defaults={
            "precio_mensual": Decimal("50.00"),
            "precio_anual": Decimal("500.00"),
            "max_usuarios": 10,
            "max_clientes": 200,
            "max_productos": 100,
            "activo": True
        }
    )
    return plan

@pytest.fixture
def empresa_fixture(plan_pro):
    emp = make_empresa()
    activar_modulo(emp, "billing")
    activar_modulo(emp, "ventas")
    activar_modulo(emp, "facturacion")
    activar_modulo(emp, "auditlog")
    
    # Update the subscription created by make_empresa instead of creating a new one
    Suscripcion.objects.filter(empresa=emp).update(
        plan=plan_pro,
        estado="ACTIVE",
        fecha_inicio=timezone.now().date()
    )
    return emp

@pytest.fixture
def metodo_pago(empresa_fixture):
    from modules.ventas.models import MetodoPago
    return MetodoPago.objects.create(
        empresa=empresa_fixture,
        nombre="Efectivo",
        tipo="EFECTIVO",
        activo=True
    )

@pytest.fixture
def admin_user_fixture(empresa_fixture):
    return make_admin(empresa_fixture)
