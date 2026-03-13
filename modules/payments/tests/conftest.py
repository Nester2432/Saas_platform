import pytest
from rest_framework.test import APIClient
from decimal import Decimal
from modules.inventario.tests.factories import make_empresa, make_admin
from modules.billing.models import Plan

@pytest.fixture
def api_client():
    client = APIClient()
    return client

@pytest.fixture
def plan_starter():
    plan, _ = Plan.objects.get_or_create(
        nombre="Starter",
        defaults={
            "precio_mensual": Decimal("0.00"),
            "max_usuarios": 1,
            "max_clientes": 10,
            "max_productos": 10,
            "activo": True,
            "stripe_price_id": "price_starter"
        }
    )
    return plan

@pytest.fixture
def empresa_demo(plan_starter):
    emp = make_empresa()
    emp.nombre = "Empresa Demo"
    emp.save()
    return emp

@pytest.fixture
def authenticated_client(api_client, empresa_demo):
    admin = make_admin(empresa_demo)
    api_client.force_authenticate(user=admin)
    # The TenantMiddleware uses this header as a strategy
    api_client.credentials(HTTP_X_EMPRESA_ID=str(empresa_demo.id))
    return api_client
