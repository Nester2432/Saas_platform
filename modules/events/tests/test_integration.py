import pytest
from decimal import Decimal
from django.db import transaction
from modules.ventas.services.ventas import VentaService
from modules.ventas.models import Venta
from modules.facturacion.models import Factura
from modules.auditlog.models import AuditLog
from modules.billing.models import UsoMensual
from modules.events.event_bus import EventBus
from apps.usuarios.models import Usuario
from apps.empresas.models import Empresa

@pytest.mark.django_db(transaction=True)
class TestEventIntegration:
    from unittest.mock import patch

    @patch("modules.notificaciones.services.notificacion_service.NotificacionService.enviar_confirmacion_venta")
    def test_venta_confirmada_flow(self, mock_confirmacion, empresa_fixture, admin_user_fixture, metodo_pago):
        """
        Verify that confirming a sale triggers the full event circuit:
        Venta -> EventBus -> [Factura, AuditLog, BillingUsage]
        """
        from django.utils import timezone
        # 1. Create BORRADOR Sale via Service (triggers VENTA_CREADA)
        with transaction.atomic():
            venta = VentaService.crear_venta(
                empresa=empresa_fixture,
                usuario=admin_user_fixture,
                pago_diferido=False
            )
            
            # Add line via Service
            VentaService.agregar_linea(
                empresa=empresa_fixture,
                venta=venta,
                descripcion="Test Item",
                precio_unitario=Decimal("1000"),
                cantidad=1,
                usuario=admin_user_fixture
            )
        
        # Prep payment
        pagos = [{
            "metodo_pago": metodo_pago,
            "monto": Decimal("1000"),
            "referencia": "PAY-123",
            "fecha": timezone.now()
        }]
        
        # 2. Confirm Venta via Service (triggers VENTA_CONFIRMADA)
        with transaction.atomic():
            VentaService.confirmar_venta(empresa_fixture, venta, pagos=pagos, usuario=admin_user_fixture)
            
        # 3. Verify results
        # AuditLog
        audits = AuditLog.objects.filter(empresa=empresa_fixture)
        assert audits.count() >= 2  # venta_creada, venta_confirmada
            
        audit = audits.filter(accion="venta_confirmada").first()
        assert audit is not None
        assert audit.recurso_id == str(venta.id)
        
        # Factura (Automated)
        factura = Factura.objects.filter(venta=venta).first()
        assert factura is not None
        assert factura.total == venta.total
        
        # Billing Usage
        uso = UsoMensual.objects.filter(empresa=empresa_fixture).first()
        assert uso is not None
        assert uso.ventas_creadas >= 1

        mock_confirmacion.assert_called_once_with(
            empresa_id=str(empresa_fixture.id),
            venta_id=str(venta.id)
        )

    @patch("modules.notificaciones.services.notificacion_service.NotificacionService.enviar_bienvenida_cliente")
    def test_cliente_creado_flow(self, mock_bienvenida, empresa_fixture, admin_user_fixture):
        from modules.clientes.services import ClienteService
        
        datos_cliente = {
            "nombre": "Test",
            "apellido": "Cliente",
            "email": "test@example.com",
            "telefono": "123456789"
        }
        
        with transaction.atomic():
            cliente = ClienteService.crear_cliente(
                empresa_fixture,
                datos_cliente,
                admin_user_fixture
            )
            
        mock_bienvenida.assert_called_once_with(
            empresa_id=str(empresa_fixture.id),
            cliente_id=str(cliente.id)
        )
