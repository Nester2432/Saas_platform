import pytest
from rest_framework.test import APIClient
from decimal import Decimal
from django.utils import timezone
from modules.inventario.tests.factories import make_empresa, make_admin, make_usuario, activar_modulo
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
def empresa(plan_pro):
    emp = make_empresa()
    activar_modulo(emp, "billing")
    activar_modulo(emp, "ventas")
    Suscripcion.objects.filter(empresa=emp).update(plan=plan_pro, estado="ACTIVE", fecha_inicio=timezone.now().date())
    return emp

@pytest.fixture
def empresa_secundaria(plan_pro):
    emp = make_empresa(nombre="Empresa Secundaria")
    activar_modulo(emp, "billing")
    Suscripcion.objects.filter(empresa=emp).update(plan=plan_pro, estado="ACTIVE", fecha_inicio=timezone.now().date())
    return emp

@pytest.fixture
def usuario_admin(empresa):
    return make_admin(empresa)

@pytest.fixture
def usuario_empleado(empresa):
    return make_usuario(empresa)

@pytest.fixture
def api_client():
    return APIClient()
