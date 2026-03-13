import threading
from decimal import Decimal
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from django.conf import settings
import pytest

from modules.ventas.services import VentaService
from modules.ventas.models import Venta, EstadoVenta, LineaVenta
from modules.inventario.models import Producto, MovimientoStock, TipoMovimiento
from modules.inventario.services import MovimientoService
from modules.inventario.exceptions import StockInsuficienteError
from modules.inventario.tests.factories import (
    make_empresa,
    make_admin,
    make_producto,
    setup_producto_con_stock,
)

class TestIntegracionVentasInventario(TransactionTestCase):
    """
    End-to-End integration tests for Ventas -> Inventario.
    Verifies that stock is deducted, restored, and rolled back correctly.
    """

    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=10)
        self.empresa = ctx["empresa"]
        self.admin = ctx["admin"]
        self.producto = ctx["producto"]

    def test_venta_confirmada_descuenta_stock(self):
        """Confirming a sale must create a SALIDA movement and reduce stock_actual."""
        venta = VentaService.crear_venta(empresa=self.empresa, usuario=self.admin, pago_diferido=True)
        VentaService.agregar_linea(
            empresa=self.empresa,
            venta=venta,
            producto=self.producto,
            cantidad=3,
            usuario=self.admin
        )
        
        VentaService.confirmar_venta(empresa=self.empresa, venta=venta, usuario=self.admin)
        
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 7)
        
        # Verify movement exists
        mov = MovimientoStock.objects.filter(
            empresa=self.empresa,
            producto=self.producto,
            referencia_tipo="venta",
            referencia_id=venta.id
        ).first()
        self.assertIsNotNone(mov)
        self.assertEqual(mov.tipo, TipoMovimiento.SALIDA)
        self.assertEqual(mov.cantidad, 3)

    def test_rollback_atomico_por_stock_insuficiente(self):
        """If one line fails due to stock, the entire sale confirmation must rollback."""
        # Producto A has 10 (self.producto)
        # Producto B has 2
        producto_b = make_producto(self.empresa, nombre="Prod B", stock_actual=2)
        
        venta = VentaService.crear_venta(empresa=self.empresa, usuario=self.admin, pago_diferido=True)
        VentaService.agregar_linea(
            empresa=self.empresa,
            venta=venta,
            producto=self.producto,
            cantidad=5,
            usuario=self.admin
        )
        VentaService.agregar_linea(
            empresa=self.empresa,
            venta=venta,
            producto=producto_b,
            cantidad=10, # Exceeds stock of B
            usuario=self.admin
        )
        
        with self.assertRaises(StockInsuficienteError):
            VentaService.confirmar_venta(empresa=self.empresa, venta=venta, usuario=self.admin)
            
        # Verify rollback: Producto A stock must STILL be 10
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 10)
        
        # Venta must still be in BORRADOR
        venta.refresh_from_db()
        self.assertEqual(venta.estado, EstadoVenta.BORRADOR)
        
        # No movements should exist for this sale
        self.assertFalse(MovimientoStock.objects.filter(referencia_id=venta.id).exists())

    def test_cancelacion_restaura_stock(self):
        """Cancelling a confirmed sale must restore stock via DEVOLUCION movement."""
        venta = VentaService.crear_venta(empresa=self.empresa, usuario=self.admin, pago_diferido=True)
        VentaService.agregar_linea(
            empresa=self.empresa,
            venta=venta,
            producto=self.producto,
            cantidad=4,
            usuario=self.admin
        )
        VentaService.confirmar_venta(empresa=self.empresa, venta=venta, usuario=self.admin)
        
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 6)
        
        VentaService.cancelar_venta(empresa=self.empresa, venta=venta, motivo="Prueba", usuario=self.admin)
        
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 10)
        
        # Verify DEVOLUCION movement
        mov = MovimientoStock.objects.filter(
            empresa=self.empresa,
            producto=self.producto,
            tipo=TipoMovimiento.DEVOLUCION,
            referencia_id=venta.id
        ).first()
        self.assertIsNotNone(mov)

    def test_idempotencia_confirmarcion(self):
        """Confirming an already confirmed sale must raise an error and not double-deduct."""
        venta = VentaService.crear_venta(empresa=self.empresa, usuario=self.admin, pago_diferido=True)
        VentaService.agregar_linea(self.empresa, venta, producto=self.producto, cantidad=2)
        VentaService.confirmar_venta(self.empresa, venta, usuario=self.admin)
        
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 8)
        
        with self.assertRaises(Exception): # TransicionVentaInvalidaError
            VentaService.confirmar_venta(self.empresa, venta, usuario=self.admin)
            
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 8) # No double deduction

    import unittest
    
    @unittest.skip("SQLite locking limitations cause false positive test failures in CI.")
    def test_concurrencia_simple_ventas_mismo_producto(self):
        """
        Two concurrent sales of 6 each on a stock of 10.
        One must succeed, the other must fail with StockInsuficienteError.
        """
        results = []
        errors = []
        
        venta1 = VentaService.crear_venta(empresa=self.empresa, usuario=self.admin, pago_diferido=True)
        VentaService.agregar_linea(self.empresa, venta1, producto=self.producto, cantidad=6)
        
        venta2 = VentaService.crear_venta(empresa=self.empresa, usuario=self.admin, pago_diferido=True)
        VentaService.agregar_linea(self.empresa, venta2, producto=self.producto, cantidad=6)
        
        def confirmar(v):
            try:
                VentaService.confirmar_venta(self.empresa, v, usuario=self.admin)
                results.append(True)
            except StockInsuficienteError:
                results.append(False)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=confirmar, args=(venta1,))
        t2 = threading.Thread(target=confirmar, args=(venta2,))
        
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)
        
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 4)
