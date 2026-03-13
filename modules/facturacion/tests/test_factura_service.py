import pytest
from django.utils import timezone
from modules.facturacion.services.facturacion import FacturaService
from modules.facturacion.models import Factura, EstadoFactura
from modules.facturacion.exceptions import FacturaActivaError, FacturaEmitidaError
from modules.inventario.tests.factories import make_empresa, make_admin, activar_modulo
from modules.ventas.models import Venta, LineaVenta, EstadoVenta
from modules.inventario.models import Producto, CategoriaProducto

@pytest.fixture
def setup_data():
    empresa = make_empresa(nombre="Factura Corp")
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
    
    return empresa, admin, venta, producto

@pytest.mark.django_db
class TestFacturaService:
    def test_generar_factura_desde_venta_ok(self, setup_data):
        empresa, admin, venta, producto = setup_data
        
        factura = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        
        assert factura.estado == EstadoFactura.BORRADOR
        assert factura.total == venta.total
        assert factura.lineas.count() == 1
        assert factura.lineas.first().descripcion == "Laptop"

    def test_no_permitir_dos_facturas_activas(self, setup_data):
        empresa, admin, venta, producto = setup_data
        
        FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        
        with pytest.raises(FacturaActivaError):
            FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)

    def test_permitir_factura_si_anterior_esta_anulada(self, setup_data):
        empresa, admin, venta, producto = setup_data
        
        f1 = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        FacturaService.anular_factura(empresa, f1, usuario=admin)
        
        # Ahora debería permitir crear otra
        f2 = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        assert f2.id != f1.id
        assert f2.estado == EstadoFactura.BORRADOR

    def test_emitir_factura_asigna_numero(self, setup_data):
        empresa, admin, venta, producto = setup_data
        
        factura = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        factura = FacturaService.emitir_factura(empresa, factura, usuario=admin)
        
        assert factura.estado == EstadoFactura.EMITIDA
        assert factura.numero.startswith("0001-")
        assert factura.fecha_emision == timezone.now().date()

    def test_no_permitir_emitir_dos_veces(self, setup_data):
        empresa, admin, venta, producto = setup_data
        
        factura = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        FacturaService.emitir_factura(empresa, factura, usuario=admin)
        
        with pytest.raises(FacturaEmitidaError):
            FacturaService.emitir_factura(empresa, factura, usuario=admin)

    def test_anular_factura_ok(self, setup_data):
        empresa, admin, venta, producto = setup_data
        
        factura = FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        FacturaService.emitir_factura(empresa, factura, usuario=admin)
        
        FacturaService.anular_factura(empresa, factura, usuario=admin)
        assert factura.estado == EstadoFactura.ANULADA
