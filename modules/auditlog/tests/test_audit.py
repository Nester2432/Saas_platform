import pytest
from django.urls import reverse
from rest_framework import status
from modules.auditlog.models import AuditLog
from modules.auditlog.services.audit_service import AuditService
from modules.ventas.services.ventas import VentaService
from modules.inventario.services.movimientos import MovimientoService
from modules.inventario.tests.factories import make_producto

@pytest.mark.django_db
class TestAuditService:
    def test_registrar_evento_basic(self, empresa, usuario_admin):
        """Verify basic event registration."""
        log = AuditService.registrar_evento(
            empresa=empresa,
            usuario=usuario_admin,
            accion="test_action",
            recurso="test_resource",
            recurso_id="123",
            metadata={"key": "value"}
        )
        
        assert log is not None
        assert log.accion == "test_action"
        assert log.metadata == {"key": "value"}
        assert log.recurso_id == "123"
        assert log.empresa == empresa

    def test_registrar_evento_with_request(self, empresa, usuario_admin, rf):
        """Verify IP and User-Agent extraction from request."""
        request = rf.get('/')
        request.META['REMOTE_ADDR'] = '192.168.1.1'
        request.META['HTTP_USER_AGENT'] = 'TestBot'
        
        log = AuditService.registrar_evento(
            empresa=empresa,
            usuario=usuario_admin,
            accion="action_with_request",
            recurso="resource",
            request=request
        )
        
        assert log.ip_address == '192.168.1.1'
        assert log.user_agent == 'TestBot'

    def test_venta_service_audit(self, empresa, usuario_admin):
        """Integration test: Verify VentaService triggers audit logs."""
        from modules.inventario.tests.factories import make_producto
        
        # Use pago_diferido=True to allow confirmation without payment
        venta = VentaService.crear_venta(empresa, usuario=usuario_admin, pago_diferido=True)
        prod = make_producto(empresa, precio_venta=100)
        
        # Add stock to avoid StockInsuficienteError
        MovimientoService.registrar_entrada(
            empresa=empresa,
            producto=prod,
            cantidad=10,
            motivo="Initial stock",
            usuario=usuario_admin
        )
        
        VentaService.agregar_linea(empresa, venta, producto=prod, cantidad=1, usuario=usuario_admin)
        
        # This should trigger "confirmar_venta" audit event
        VentaService.confirmar_venta(empresa, venta, usuario=usuario_admin)
        
        # Check logs
        logs = AuditLog.objects.filter(empresa=empresa, accion="confirmar_venta")
        assert logs.count() == 1
        assert logs[0].recurso_id == str(venta.id)
        assert logs[0].usuario == usuario_admin

@pytest.mark.django_db
class TestAuditAPI:
    def test_list_audit_logs_tenant_isolation(self, api_client, empresa, empresa_secundaria, usuario_admin):
        """Verify that a user only sees logs from their own company."""
        # Create logs for both companies
        AuditService.registrar_evento(empresa, usuario_admin, "accion_empresa_1", "recurso")
        
        # Log for another company (mocked)
        AuditLog.objects.create(
            empresa=empresa_secundaria,
            accion="accion_empresa_2",
            recurso="recurso"
        )
        
        url = reverse("auditlog-list")
        api_client.force_authenticate(user=usuario_admin)
        
        # Explicitly set X-Empresa-ID for middleware
        response = api_client.get(url, HTTP_X_EMPRESA_ID=str(empresa.id))
        
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 1
        assert response.data["results"][0]["accion"] == "accion_empresa_1"

    def test_list_audit_logs_permissions(self, api_client, empresa, usuario_empleado):
        """Verify that only admins can see logs."""
        api_client.force_authenticate(user=usuario_empleado)
        url = reverse("auditlog-list")
        
        response = api_client.get(url, HTTP_X_EMPRESA_ID=str(empresa.id))
        
        # Should be forbidden for regular employee
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_filter_audit_logs(self, api_client, empresa, usuario_admin):
        """Verify API filtering by action and user."""
        AuditService.registrar_evento(empresa, usuario_admin, "crear_producto", "producto")
        AuditService.registrar_evento(empresa, usuario_admin, "eliminar_producto", "producto")
        
        api_client.force_authenticate(user=usuario_admin)
        url = reverse("auditlog-list")
        
        # Filter by action
        response = api_client.get(f"{url}?accion=crear_producto", HTTP_X_EMPRESA_ID=str(empresa.id))
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 1
        assert response.data["results"][0]["accion"] == "crear_producto"
