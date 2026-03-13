from decimal import Decimal
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from modules.pagos.models import Pago, EstadoPago
from modules.pagos.services.pagos import PagoService
from modules.pagos.exceptions import SobrePagoError, TransicionPagoInvalidaError
from modules.ventas.models import Venta, EstadoVenta, MetodoPago
from modules.ventas.services import VentaService
from modules.inventario.tests.factories import (
    make_empresa,
    make_admin,
    make_producto,
    setup_producto_con_stock,
)

class TestPagoService(TransactionTestCase):
    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=10)
        self.empresa = ctx["empresa"]
        self.admin = ctx["admin"]
        self.producto = ctx["producto"]
        
        # Create a confirmed sale with balance
        self.venta = VentaService.crear_venta(self.empresa, usuario=self.admin, pago_diferido=True)
        VentaService.agregar_linea(self.empresa, self.venta, producto=self.producto, cantidad=2)
        VentaService.confirmar_venta(self.empresa, self.venta, usuario=self.admin)
        # Venta total = 2 * precio (let's say 150*2 = 300)
        self.venta.refresh_from_db()
        self.total = self.venta.total
        
        self.metodo = MetodoPago.objects.filter(empresa=self.empresa).first()
        if not self.metodo:
            self.metodo = MetodoPago.objects.create(
                empresa=self.empresa,
                nombre="Efectivo",
                tipo="EFECTIVO"
            )

    def test_registrar_pago_pendiente(self):
        pago = PagoService.registrar_pago(
            self.empresa, self.venta, Decimal("100"), self.metodo, usuario=self.admin
        )
        self.assertEqual(pago.estado, EstadoPago.PENDIENTE)
        self.assertEqual(pago.monto, Decimal("100"))
        # Venta should still have 0 paid in PagoVenta
        self.assertEqual(self.venta.pagos.count(), 0)

    def test_confirmar_pago_exitoso(self):
        pago = PagoService.registrar_pago(self.empresa, self.venta, Decimal("100"), self.metodo)
        PagoService.confirmar_pago(self.empresa, pago, usuario=self.admin)
        
        pago.refresh_from_db()
        self.assertEqual(pago.estado, EstadoPago.CONFIRMADO)
        
        # Check Venta integration
        self.venta.refresh_from_db()
        self.assertEqual(self.venta.pagos.count(), 1)
        self.assertEqual(self.venta.pagos.first().monto, Decimal("100"))
        self.assertEqual(self.venta.estado, EstadoVenta.CONFIRMADA) # Still has balance

    def test_sobrepago_error(self):
        # Total is 300 (approx, depends on make_producto default price)
        # Let's use exact monto
        monto_excesivo = self.total + Decimal("1")
        pago = PagoService.registrar_pago(self.empresa, self.venta, monto_excesivo, self.metodo)
        
        with self.assertRaises(SobrePagoError):
            PagoService.confirmar_pago(self.empresa, pago, usuario=self.admin)

    def test_multiple_pagos_completa_venta(self):
        # Pago 1
        p1 = PagoService.registrar_pago(self.empresa, self.venta, self.total / 2, self.metodo)
        PagoService.confirmar_pago(self.empresa, p1, usuario=self.admin)
        
        self.venta.refresh_from_db()
        self.assertEqual(self.venta.estado, EstadoVenta.CONFIRMADA)
        
        # Pago 2 (completes)
        p2 = PagoService.registrar_pago(self.empresa, self.venta, self.total / 2, self.metodo)
        PagoService.confirmar_pago(self.empresa, p2, usuario=self.admin)
        
        self.venta.refresh_from_db()
        self.assertEqual(self.venta.estado, EstadoVenta.PAGADA)
        self.assertEqual(self.venta.pagos.count(), 2)

    def test_fallar_pago(self):
        pago = PagoService.registrar_pago(self.empresa, self.venta, Decimal("50"), self.metodo)
        PagoService.fallar_pago(self.empresa, pago, usuario=self.admin)
        
        pago.refresh_from_db()
        self.assertEqual(pago.estado, EstadoPago.FALLIDO)

    def test_reembolsar_pago(self):
        pago = PagoService.registrar_pago(self.empresa, self.venta, Decimal("50"), self.metodo)
        PagoService.confirmar_pago(self.empresa, pago, usuario=self.admin)
        
        PagoService.reembolsar_pago(self.empresa, pago, usuario=self.admin)
        pago.refresh_from_db()
        self.assertEqual(pago.estado, EstadoPago.REEMBOLSADO)
