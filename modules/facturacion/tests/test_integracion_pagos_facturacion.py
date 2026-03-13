import pytest
from decimal import Decimal
from django.utils import timezone
from django.db import transaction
from modules.inventario.tests.factories import make_empresa, make_admin, activar_modulo
from modules.ventas.models import Venta, LineaVenta, EstadoVenta, PagoVenta, MetodoPago, TipoMetodoPago
from modules.ventas.services import VentaService
from modules.pagos.services.pagos import PagoService
from modules.pagos.models import Pago, EstadoPago
from modules.facturacion.models import Factura, EstadoFactura
from modules.inventario.models import Producto, CategoriaProducto

@pytest.fixture
def setup_integracion():
    empresa = make_empresa(nombre="Integracion Corp")
    admin = make_admin(empresa)
    activar_modulo(empresa, "ventas")
    activar_modulo(empresa, "inventario")
    activar_modulo(empresa, "pagos")
    activar_modulo(empresa, "facturacion")
    
    categoria = CategoriaProducto.objects.create(empresa=empresa, nombre="Hardware")
    producto = Producto.objects.create(
        empresa=empresa,
        categoria=categoria,
        nombre="Monitor 4K",
        precio_venta=Decimal("500.00"),
        stock_actual=20
    )
    
    venta = Venta.objects.create(
        empresa=empresa,
        numero="V-2026-0001",
        total=Decimal("500.00"),
        subtotal=Decimal("500.00"),
        estado=EstadoVenta.CONFIRMADA,
        fecha=timezone.now()
    )
    LineaVenta.objects.create(
        empresa=empresa,
        venta=venta,
        producto=producto,
        descripcion="Monitor 4K",
        cantidad=1,
        precio_unitario=Decimal("500.00"),
        subtotal=Decimal("500.00")
    )
    
    metodo_pago = MetodoPago.objects.create(
        empresa=empresa,
        nombre="Efectivo",
        tipo=TipoMetodoPago.EFECTIVO
    )
    
    return empresa, admin, venta, producto, metodo_pago

@pytest.mark.django_db
class TestIntegracionPagosFacturacion:
    def test_pago_total_genera_factura_automaticamente(self, setup_integracion):
        empresa, admin, venta, producto, metodo_pago = setup_integracion
        
        # 1. Registrar un pago por el total
        pago = PagoService.registrar_pago(
            empresa=empresa,
            venta=venta,
            monto=Decimal("500.00"),
            metodo_pago=metodo_pago,
            usuario=admin
        )
        
        # 2. Confirmar el pago
        PagoService.confirmar_pago(empresa, pago, usuario=admin)
        
        # 3. Verificar que la venta está PAGADA
        venta.refresh_from_db()
        assert venta.estado == EstadoVenta.PAGADA
        
        # 4. Verificar que se generó la factura automáticamente
        factura = Factura.objects.get(venta=venta)
        assert factura.estado == EstadoFactura.BORRADOR
        assert factura.total == Decimal("500.00")
        assert factura.lineas.count() == 1
        assert factura.lineas.first().descripcion == "Monitor 4K"

    def test_pago_parcial_no_genera_factura(self, setup_integracion):
        empresa, admin, venta, producto, metodo_pago = setup_integracion
        
        # 1. Registrar pago parcial
        pago = PagoService.registrar_pago(
            empresa=empresa,
            venta=venta,
            monto=Decimal("200.00"),
            metodo_pago=metodo_pago,
            usuario=admin
        )
        PagoService.confirmar_pago(empresa, pago, usuario=admin)
        
        # 2. Verificar venta sigue CONFIRMADA
        venta.refresh_from_db()
        assert venta.estado == EstadoVenta.CONFIRMADA
        
        # 3. Verificar que NO hay factura
        assert not Factura.objects.filter(venta=venta).exists()

    def test_no_duplicar_factura_si_ya_existe(self, setup_integracion):
        empresa, admin, venta, producto, metodo_pago = setup_integracion
        
        # 1. Crear factura previa manual
        from modules.facturacion.services.facturacion import FacturaService
        FacturaService.generar_factura_desde_venta(empresa, venta, usuario=admin)
        assert Factura.objects.filter(venta=venta).count() == 1
        
        # 2. Completar pago
        pago = PagoService.registrar_pago(
            empresa=empresa,
            venta=venta,
            monto=Decimal("500.00"),
            metodo_pago=metodo_pago,
            usuario=admin
        )
        PagoService.confirmar_pago(empresa, pago, usuario=admin)
        
        # 3. Verificar que sigue habiendo SOLO UNA factura
        assert Factura.objects.filter(venta=venta).count() == 1

    def test_rollback_si_falla_facturacion(self, setup_integracion, monkeypatch):
        empresa, admin, venta, producto, metodo_pago = setup_integracion
        
        # Forzar un error en la generación de factura
        from modules.facturacion.services.facturacion import FacturaService
        def fail_generar(*args, **kwargs):
            raise Exception("Error catastrófico en facturación")
        
        monkeypatch.setattr(FacturaService, "generar_factura_desde_venta", fail_generar)
        
        # Intentar confirmar pago
        pago = PagoService.registrar_pago(
            empresa=empresa,
            venta=venta,
            monto=Decimal("500.00"),
            metodo_pago=metodo_pago,
            usuario=admin
        )
        
        with pytest.raises(Exception, match="Error catastrófico en facturación"):
            PagoService.confirmar_pago(empresa, pago, usuario=admin)
            
        # Verificar ROLLBACK
        pago.refresh_from_db()
        assert pago.estado == EstadoPago.PENDIENTE # Volvió a pendiente
        
        venta.refresh_from_db()
        assert venta.estado == EstadoVenta.CONFIRMADA # No pasó a PAGADA
        assert venta.pagos.count() == 0 # El PagoVenta no se registró
