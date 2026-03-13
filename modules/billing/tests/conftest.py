import pytest
from rest_framework.test import APIClient
from decimal import Decimal
from datetime import date, timedelta
from django.utils import timezone
from modules.inventario.tests.factories import make_empresa, make_admin, activar_modulo
from modules.billing.models import Plan, Suscripcion

@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()

@pytest.fixture
def setup_billing():
    empresa = make_empresa(nombre="Billing Corp")
    admin = make_admin(empresa)
    activar_modulo(empresa, "billing")
    activar_modulo(empresa, "inventario") # Needed for product tests
    activar_modulo(empresa, "clientes")
    
    # Create Plans
    starter = Plan.objects.create(
        nombre="Starter",
        precio_mensual=Decimal("19.99"),
        max_usuarios=2,
        max_productos=5,
        max_clientes=3
    )
    
    enterprise = Plan.objects.create(
        nombre="Enterprise",
        precio_mensual=Decimal("99.99"),
        max_usuarios=None, # Unlimited
        max_productos=None,
        max_clientes=None
    )
    
    from modules.billing.services.billing_service import BillingService
    # Upgrade current (trial) subscription to Starter
    sub = BillingService.upgrade_plan(empresa, starter)
    sub.fecha_fin = timezone.now().date() + timedelta(days=30)
    sub.save()
    
    return empresa, admin, starter, enterprise, sub
