import pytest
from datetime import date, timedelta
from decimal import Decimal
from django.utils import timezone
from django.core.exceptions import ValidationError
from modules.inventario.tests.factories import make_empresa, make_admin, activar_modulo
from modules.billing.models import Plan, Suscripcion, UsoMensual
from modules.billing.services.billing_service import BillingService


@pytest.mark.django_db
class TestBillingLimits:
    def test_check_plan_limits_productos_excedido(self, setup_billing):
        empresa, _, starter, _, _ = setup_billing
        from modules.inventario.models import Producto, CategoriaProducto
        
        cat = CategoriaProducto.objects.create(empresa=empresa, nombre="Test")
        # Crear 5 productos (límite del plan Starter)
        for i in range(5):
            Producto.objects.create(empresa=empresa, categoria=cat, nombre=f"P{i}", precio_venta=Decimal("10.00"))
            
        with pytest.raises(ValidationError, match="Límite de productos alcanzado"):
            Producto.objects.create(empresa=empresa, categoria=cat, nombre=f"P5", precio_venta=Decimal("10.00"))

    def test_check_plan_limits_clientes_excedido(self, setup_billing):
        empresa, _, starter, _, _ = setup_billing
        from modules.clientes.models import Cliente
        
        # Crear 3 clientes (límite del plan Starter)
        for i in range(3):
            Cliente.objects.create(empresa=empresa, nombre=f"C{i}", email=f"c{i}@test.com")
            
        with pytest.raises(ValidationError, match="Límite de clientes alcanzado"):
            BillingService.check_plan_limits(empresa, "clientes")

    def test_check_plan_limits_ilimitado(self, setup_billing):
        empresa, _, _, enterprise, sub = setup_billing
        
        # Upgrade a Enterprise
        BillingService.upgrade_plan(empresa, enterprise)
        
        # Simulamos muchos clientes
        from modules.clientes.models import Cliente
        for i in range(10):
            Cliente.objects.create(empresa=empresa, nombre=f"C{i}", email=f"exp{i}@test.com")
        
        # Debería pasar sin error
        BillingService.check_plan_limits(empresa, "clientes")

    def test_subscription_expired(self, setup_billing):
        empresa, _, _, _, sub = setup_billing
        
        # Expirar suscripción
        sub.fecha_fin = timezone.now().date() - timedelta(days=1)
        sub.save()
        
        with pytest.raises(ValidationError, match="ha expirado"):
            BillingService.check_plan_limits(empresa, "clientes")

    def test_automatic_trial_on_empresa_creation(self):
        from apps.empresas.models import Empresa
        # Crear un plan para el trial
        Plan.objects.create(nombre="Free", precio_mensual=0, max_usuarios=1)
        
        empresa = Empresa.objects.create(nombre="New Startup")
        
        sub = Suscripcion.objects.filter(empresa=empresa).first()
        assert sub is not None
        assert sub.estado == "TRIAL"
        assert sub.fecha_fin == sub.fecha_inicio + timedelta(days=14)
