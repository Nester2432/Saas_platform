import pytest
from decimal import Decimal
from django.utils import timezone
from modules.inventario.tests.factories import make_empresa, make_admin, activar_modulo
from modules.ventas.models import Venta, EstadoVenta, LineaVenta
from modules.facturacion.models import Factura, EstadoFactura, PuntoVenta, SecuenciaComprobante, TipoComprobante
from modules.facturacion.services.facturacion import FacturaService
from django.core.exceptions import ValidationError
from modules.facturacion.exceptions import FacturaEmitidaError, FacturacionError

@pytest.fixture
def setup_fiscal():
    empresa = make_empresa(nombre="Fiscal Corp")
    admin = make_admin(empresa)
    activar_modulo(empresa, "ventas")
    activar_modulo(empresa, "facturacion")
    
    # El Punto de Venta 0001 se crea automáticamente por el signal
    pv1 = PuntoVenta.objects.get(empresa=empresa, codigo="0001")
    
    # Crear un segundo Punto de Venta
    pv2 = PuntoVenta.objects.create(
        empresa=empresa,
        codigo="0002",
        descripcion="Sucursal Norte"
    )
    
    venta = Venta.objects.create(
        empresa=empresa,
        numero="V-F1",
        total=1000,
        subtotal=1000,
        estado=EstadoVenta.CONFIRMADA,
        fecha=timezone.now()
    )
    
    return empresa, admin, pv1, pv2, venta

@pytest.mark.django_db
class TestNumeracionFiscal:
    def test_creacion_automatica_pv_0001(self):
        empresa = make_empresa(nombre="Auto PV Corp")
        assert PuntoVenta.objects.filter(empresa=empresa, codigo="0001").exists()

    def test_emision_correlativa_por_pv_y_tipo(self, setup_fiscal):
        empresa, admin, pv1, pv2, venta = setup_fiscal
        
        # Factura 1 (Punto 0001, Tipo B)
        f1 = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        f1 = FacturaService.emitir_factura(empresa, f1, pv1, usuario=admin)
        assert f1.numero == "0001-00000001"
        assert f1.numero_secuencial == 1
        
        # Factura 2 (Punto 0001, Tipo B)
        # Necesitamos otra venta porque hay validación de una factura activa por venta
        v2 = Venta.objects.create(
            empresa=empresa, 
            numero="V-F2", 
            total=100, 
            subtotal=100, 
            estado=EstadoVenta.CONFIRMADA,
            fecha=timezone.now()
        )
        f2 = FacturaService.generar_factura_desde_venta(empresa, v2, usuario=admin)
        f2 = FacturaService.emitir_factura(empresa, f2, pv1, usuario=admin)
        assert f2.numero == "0001-00000002"
        
        # Factura 3 (Punto 0002, Tipo B)
        v3 = Venta.objects.create(
            empresa=empresa, 
            numero="V-F3", 
            total=100, 
            subtotal=100, 
            estado=EstadoVenta.CONFIRMADA,
            fecha=timezone.now()
        )
        f3 = FacturaService.generar_factura_desde_venta(empresa, v3, usuario=admin)
        f3 = FacturaService.emitir_factura(empresa, f3, pv2, usuario=admin)
        assert f3.numero == "0002-00000001"
        
        # Factura 4 (Punto 0001, Tipo A)
        v4 = Venta.objects.create(
            empresa=empresa, 
            numero="V-F4", 
            total=100, 
            subtotal=100, 
            estado=EstadoVenta.CONFIRMADA,
            fecha=timezone.now()
        )
        f4 = FacturaService.generar_factura_desde_venta(empresa, v4, usuario=admin)
        f4.tipo = TipoComprobante.A
        f4.save()
        f4 = FacturaService.emitir_factura(empresa, f4, pv1, usuario=admin)
        assert f4.numero == "0001-00000001" # Secuencia independiente para Tipo A

    def test_error_al_emitir_dos_veces(self, setup_fiscal):
        empresa, admin, pv1, pv2, venta = setup_fiscal
        f = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        FacturaService.emitir_factura(empresa, f, pv1, usuario=admin)
        
        with pytest.raises(FacturaEmitidaError, match="estado BORRADOR"):
            FacturaService.emitir_factura(empresa, f, pv1, usuario=admin)

    def test_error_al_emitir_sin_pv_activo(self, setup_fiscal):
        empresa, admin, pv1, pv2, venta = setup_fiscal
        pv1.activo = False
        pv1.save()
        
        f = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        with pytest.raises(ValidationError, match="no está activo"):
            FacturaService.emitir_factura(empresa, f, pv1, usuario=admin)

    def test_concurrencia_emision(self, setup_fiscal):
        # Este test simula concurrencia si el backend lo soporta, 
        # pero para SQLite solo validamos que no haya errores de lógica básica
        empresa, admin, pv1, pv2, venta = setup_fiscal
        
        from django.db import transaction
        
        # Simular dos hilos intentando emitir capturando el lock
        with transaction.atomic():
            f1 = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
            # En un entorno real, otra transacción se bloquearía aquí
            f1 = FacturaService.emitir_factura(empresa, f1, pv1, usuario=admin)
            assert f1.numero_secuencial == 1

    def test_codigo_unico_por_empresa(self, setup_fiscal):
        empresa, admin, pv1, pv2, venta = setup_fiscal
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            PuntoVenta.objects.create(empresa=empresa, codigo="0001")
