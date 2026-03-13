import pytest
from rest_framework.test import APIClient
from rest_framework import status
from django.urls import reverse
from django.core.cache import cache
from django.test import override_settings
from modules.inventario.tests.factories import make_empresa, make_admin, activar_modulo
from modules.ventas.models import Venta, LineaVenta, EstadoVenta
from modules.inventario.models import Producto, CategoriaProducto
from modules.facturacion.models import Factura, EstadoFactura

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def setup_api_data():
    empresa = make_empresa(nombre="Factura API Corp")
    admin = make_admin(empresa)
    activar_modulo(empresa, "ventas")
    activar_modulo(empresa, "inventario")
    activar_modulo(empresa, "facturacion")
    
    categoria = CategoriaProducto.objects.create(empresa=empresa, nombre="General")
    producto = Producto.objects.create(
        empresa=empresa, 
        categoria=categoria, 
        nombre="Laptop", 
        precio_venta=1000,
        stock_actual=10
    )
    
    from django.utils import timezone
    venta = Venta.objects.create(
        empresa=empresa, 
        numero="V-2000-0001",
        total=1000, 
        subtotal=1000,
        estado=EstadoVenta.CONFIRMADA,
        fecha=timezone.now()
    )
    LineaVenta.objects.create(
        empresa=empresa,
        venta=venta, 
        producto=producto, 
        descripcion="Laptop", 
        cantidad=1, 
        precio_unitario=1000, 
        subtotal=1000
    )
    
    return empresa, admin, venta

@pytest.mark.django_db
class TestFacturacionAPI:
    def test_generar_desde_venta_api(self, api_client, setup_api_data, settings):
        settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
        empresa, admin, venta = setup_api_data
        api_client.force_authenticate(user=admin)
        
        # We need the header for the middleware
        url = reverse("factura-generar-desde-venta", kwargs={"venta_id": venta.id})
        response = api_client.post(url, HTTP_X_EMPRESA_ID=str(empresa.id))
        
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["venta"] == venta.id
        assert response.data["estado"] == "BORRADOR"

    def test_emitir_factura_api(self, api_client, setup_api_data, settings):
        settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
        empresa, admin, venta = setup_api_data
        api_client.force_authenticate(user=admin)
        
        # Primero crear borrador
        factura = Factura.objects.create(
            empresa=empresa, 
            venta=venta, 
            estado=EstadoFactura.BORRADOR, 
            total=1000
        )
        
        url = reverse("factura-emitir", kwargs={"pk": factura.id})
        response = api_client.post(url, HTTP_X_EMPRESA_ID=str(empresa.id))
        
        assert response.status_code == status.HTTP_200_OK
        assert response.data["estado"] == "EMITIDA"
        assert response.data["numero"] is not None

    def test_anular_factura_api(self, api_client, setup_api_data, settings):
        settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
        empresa, admin, venta = setup_api_data
        api_client.force_authenticate(user=admin)
        
        factura = Factura.objects.create(
            empresa=empresa, 
            venta=venta, 
            estado=EstadoFactura.EMITIDA, 
            total=1000,
            numero="0001-00000001"
        )
        
        url = reverse("factura-anular", kwargs={"pk": factura.id})
        response = api_client.post(url, HTTP_X_EMPRESA_ID=str(empresa.id))
        
        assert response.status_code == status.HTTP_200_OK
        assert response.data["estado"] == "ANULADA"

    def test_list_facturas_tenant_isolation(self, api_client, setup_api_data, settings):
        settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
        empresa1, admin1, venta1 = setup_api_data
        
        empresa2 = make_empresa(nombre="Other Corp")
        admin2 = make_admin(empresa2)
        activar_modulo(empresa2, "facturacion")
        
        Factura.objects.create(empresa=empresa1, venta=venta1, total=100, estado=EstadoFactura.EMITIDA)
        
        api_client.force_authenticate(user=admin2)
        url = reverse("factura-list")
        response = api_client.get(url, HTTP_X_EMPRESA_ID=str(empresa2.id))
        
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) == 0
