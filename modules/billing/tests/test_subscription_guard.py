import pytest
from rest_framework import status
from django.urls import reverse
from django.core.exceptions import ValidationError

from modules.billing.models import EstadoSuscripcion, Suscripcion
from modules.billing.services.billing_service import BillingService

@pytest.mark.django_db
class TestSubscriptionGuard:
    
    def test_verificar_suscripcion_fallida_si_suspendida(self, setup_billing):
        empresa, _, _, _, suscripcion = setup_billing
        
        # Suspendemos la suscripción
        suscripcion.estado = EstadoSuscripcion.SUSPENDIDA
        suscripcion.save()
        
        with pytest.raises(ValidationError, match="está actualmente Suspendida"):
            BillingService.verificar_suscripcion_activa(empresa)

    def test_middleware_permite_get_en_suspendida(self, api_client, setup_billing):
        empresa, admin, _, _, suscripcion = setup_billing
        
        # Suspendemos la suscripción
        suscripcion.estado = EstadoSuscripcion.SUSPENDIDA
        suscripcion.save()
        
        api_client.force_authenticate(user=admin)
        
        # GET debería estar permitido
        # Pasamos el header de empresa para que el TenantMiddleware y TenantQuerysetMixin funcionen
        url = reverse("producto-list")
        response = api_client.get(url, HTTP_X_EMPRESA_ID=str(empresa.id))
        
        assert response.status_code == status.HTTP_200_OK

    def test_middleware_bloquea_post_en_suspendida(self, api_client, setup_billing):
        empresa, admin, _, _, suscripcion = setup_billing
        
        # Suspendemos la suscripción
        suscripcion.estado = EstadoSuscripcion.SUSPENDIDA
        suscripcion.save()
        
        api_client.force_authenticate(user=admin)
        
        # POST debería estar bloqueado
        url = reverse("producto-list")
        data = {"nombre": "Producto Fallido", "precio_venta": "100.00"}
        response = api_client.post(url, data, HTTP_X_EMPRESA_ID=str(empresa.id))
        
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "Suscripción Inactiva" in response.json()["error"]

    def test_middleware_permite_billing_whitelisted_para_empresa_suspendida(self, api_client, setup_billing):
        empresa, admin, _, _, suscripcion = setup_billing
        
        # Suspendemos la suscripción
        suscripcion.estado = EstadoSuscripcion.SUSPENDIDA
        suscripcion.save()
        
        api_client.force_authenticate(user=admin)
        
        # El endpoint de 'planes' está en el whitelist y debería devolver 200 siempre
        url = reverse("plan-list")
        response = api_client.get(url, HTTP_X_EMPRESA_ID=str(empresa.id))
        
        assert response.status_code == status.HTTP_200_OK

    def test_superuser_puede_suspender_reactivar(self, api_client, setup_billing):
        empresa, admin, _, _, suscripcion = setup_billing
        
        # Creamos un superuser
        from apps.usuarios.models import Usuario
        super_admin = Usuario.objects.create_superuser(
            email="super@test.com", 
            password="pass", 
            nombre="Super",
            apellido="Admin"
        )
        
        api_client.force_authenticate(user=super_admin)
        
        # Suspender
        url_suspender = reverse("suscripcion-suspender", kwargs={"pk": suscripcion.id})
        response = api_client.post(url_suspender)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["estado"] == EstadoSuscripcion.SUSPENDIDA
        
        # Reactivar
        url_reactivar = reverse("suscripcion-reactivar", kwargs={"pk": suscripcion.id})
        response = api_client.post(url_reactivar)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["estado"] == EstadoSuscripcion.ACTIVA
