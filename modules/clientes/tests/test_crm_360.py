import pytest
from django.urls import reverse
from rest_framework import status
from modules.clientes.models import Cliente
from modules.ventas.models import Venta
from modules.turnos.models import Turno

@pytest.mark.django_db
class TestCRM360:
    def test_list_contactos_tenant_safety(self, api_client, empresa_factory, usuario_factory):
        # Create two empresas
        empresa_a = empresa_factory()
        empresa_b = empresa_factory()
        
        # User for empresa A
        user_a = usuario_factory(empresa=empresa_a)
        api_client.force_authenticate(user=user_a)
        
        # Client for empresa A and B
        Cliente.objects.create(empresa=empresa_a, nombre="Cliente A", email="a@test.com")
        Cliente.objects.create(empresa=empresa_b, nombre="Cliente B", email="b@test.com")
        
        url = reverse('contacto-list')
        response = api_client.get(url)
        
        assert response.status_code == status.HTTP_200_OK
        # Should only see client from empresa A
        assert len(response.data['results']) == 1
        assert response.data['results'][0]['nombre'] == "Cliente A"

    def test_detail_contacto_aggregation(self, api_client, empresa_factory, usuario_factory, cliente_factory):
        empresa = empresa_factory()
        user = usuario_factory(empresa=empresa)
        api_client.force_authenticate(user=user)
        
        cliente = cliente_factory(empresa=empresa, nombre="Juan")
        
        # Create related data
        Venta.objects.create(empresa=empresa, cliente=cliente, total=100)
        # Assuming we have a way to create turnos easily (simplified for test)
        # Turno.objects.create(...)
        
        url = reverse('contacto-detail', kwargs={'pk': cliente.id})
        response = api_client.get(url)
        
        assert response.status_code == status.HTTP_200_OK
        assert 'cliente' in response.data
        assert 'ventas' in response.data
        assert 'turnos' in response.data
        assert 'facturas' in response.data
        assert 'actividad' in response.data
        assert response.data['cliente']['nombre'] == "Juan"
        assert len(response.data['ventas']) == 1

    def test_detail_contacto_unauthorized_tenant(self, api_client, empresa_factory, usuario_factory, cliente_factory):
        empresa_a = empresa_factory()
        empresa_b = empresa_factory()
        user_a = usuario_factory(empresa=empresa_a)
        api_client.force_authenticate(user=user_a)
        
        # Client belonging to B
        cliente_b = cliente_factory(empresa=empresa_b)
        
        url = reverse('contacto-detail', kwargs={'pk': cliente_b.id})
        response = api_client.get(url)
        
        # Should be 404 because it's filtered by tenant first
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_contacto_search_filters_results(self, api_client, empresa_factory, usuario_factory):
        empresa = empresa_factory()
        user = usuario_factory(empresa=empresa)
        api_client.force_authenticate(user=user)
        
        Cliente.objects.create(empresa=empresa, nombre="Marcos", email="marcos@test.com")
        Cliente.objects.create(empresa=empresa, nombre="Lucas", email="lucas@test.com")
        
        url = reverse('contacto-list')
        response = api_client.get(url, {'search': 'Marcos'})
        
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data['results']) == 1
        assert response.data['results'][0]['nombre'] == "Marcos"
