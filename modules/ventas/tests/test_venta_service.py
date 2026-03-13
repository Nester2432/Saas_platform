"""
modules/ventas/tests/test_venta_service.py

Service-layer tests for VentaService.

Test structure:
    CrearVentaTest              → crear_venta (BORRADOR creation)
    AgregarLineaTest            → agregar_linea (line management)
    QuitarLineaTest             → quitar_linea + orden re-sequencing
    ConfirmarVentaTest          → confirmar_venta (happy paths)
    ConfirmarVentaValidacionTest → confirmar_venta (error paths)
    ConfirmarVentaStockTest     → stock integration with MovimientoService
    RegistrarPagoTest           → registrar_pago + CONFIRMADA→PAGADA transition
    CancelarVentaTest           → cancelar_venta (BORRADOR and CONFIRMADA)
    CancelarVentaStockTest      → stock restoration on cancellation
    DevolucionTest              → registrar_devolucion (partial and total)
    DevolucionStockTest         → stock restoration on return
    InvariantesVentaTest        → V1–V6 explicitly named
    TenantAislamientoTest       → cross-tenant rejection
    ConcurrenciaVentaTest       → correlativo under concurrent creation
                                  (TransactionTestCase — real commits)
    ConcurrenciaStockVentaTest  → concurrent confirmations + stock race
                                  (TransactionTestCase)
"""

import threading
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase

from modules.inventario.exceptions import StockInsuficienteError
from modules.inventario.models import MovimientoStock, TipoMovimiento
from modules.ventas.exceptions import (
    DevolucionInvalidaError,
    PagoInsuficienteError,
    TransicionVentaInvalidaError,
    VentaSinLineasError,
)
from modules.ventas.models import (
    DevolucionLineaVenta,
    DevolucionVenta,
    EstadoVenta,
    LineaVenta,
    PagoVenta,
    Venta,
)
from modules.ventas.services import VentaService
from modules.ventas.tests.factories import (
    make_admin,
    make_cliente,
    make_empresa,
    make_linea,
    make_metodo_pago,
    make_producto_con_stock,
    make_venta_borrador,
    setup_contexto_base,
    setup_venta_confirmada,
)


# ─────────────────────────────────────────────────────────────────────────────
# crear_venta
# ─────────────────────────────────────────────────────────────────────────────

class CrearVentaTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.admin       = ctx["admin"]
        self.cliente     = ctx["cliente"]

    def test_crea_venta_en_estado_borrador(self):
        """A new sale starts in BORRADOR state."""
        venta = VentaService.crear_venta(empresa=self.empresa)
        self.assertEqual(venta.estado, EstadoVenta.BORRADOR)

    def test_venta_sin_numero_en_borrador(self):
        """BORRADOR sales have no correlative number yet."""
        venta = VentaService.crear_venta(empresa=self.empresa)
        self.assertIsNone(venta.numero)

    def test_venta_con_cliente_captura_snapshot(self):
        """datos_cliente is populated from the Cliente when provided."""
        venta = VentaService.crear_venta(
            empresa=self.empresa, cliente=self.cliente
        )
        self.assertEqual(venta.cliente, self.cliente)
        self.assertIn("nombre", venta.datos_cliente)
        self.assertIn("email",  venta.datos_cliente)

    def test_venta_sin_cliente_es_anonima(self):
        """Sales without a client are anonymous (cliente=None)."""
        venta = VentaService.crear_venta(empresa=self.empresa)
        self.assertIsNone(venta.cliente)
        self.assertEqual(venta.datos_cliente, {})

    def test_venta_totales_iniciales_en_cero(self):
        """A new BORRADOR sale starts with subtotal=0 and total=0."""
        venta = VentaService.crear_venta(empresa=self.empresa)
        self.assertEqual(venta.subtotal, Decimal("0"))
        self.assertEqual(venta.total,    Decimal("0"))

    def test_cliente_otra_empresa_lanza_error(self):
        """Cannot create a sale linking a client from a different empresa."""
        otra_empresa = make_empresa()
        cliente_ajeno = make_cliente(otra_empresa)
        with self.assertRaises(ValidationError):
            VentaService.crear_venta(
                empresa=self.empresa, cliente=cliente_ajeno
            )


# ─────────────────────────────────────────────────────────────────────────────
# agregar_linea
# ─────────────────────────────────────────────────────────────────────────────

class AgregarLineaTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa  = ctx["empresa"]
        self.admin    = ctx["admin"]
        self.producto = ctx["producto"]
        self.venta    = make_venta_borrador(self.empresa)

    def test_agrega_linea_con_producto(self):
        """A line with a product auto-populates descripcion and precio."""
        linea = make_linea(
            empresa=self.empresa, venta=self.venta, producto=self.producto
        )
        self.assertEqual(linea.descripcion,     self.producto.nombre)
        self.assertEqual(linea.precio_unitario, self.producto.precio_venta)

    def test_agrega_linea_de_servicio_sin_producto(self):
        """A service line (no product) requires explicit descripcion and precio."""
        linea = make_linea(
            empresa=self.empresa, venta=self.venta,
            descripcion="Consulta profesional",
            precio_unitario=Decimal("500.00"),
        )
        self.assertIsNone(linea.producto)
        self.assertEqual(linea.descripcion, "Consulta profesional")

    def test_subtotal_linea_calculado(self):
        """LineaVenta.subtotal = (precio × cantidad) - descuento."""
        linea = make_linea(
            empresa=self.empresa, venta=self.venta,
            descripcion="Item",
            precio_unitario=Decimal("100.00"),
            cantidad=3,
            descuento=Decimal("50.00"),
        )
        # (100 × 3) - 50 = 250
        self.assertEqual(linea.subtotal, Decimal("250.00"))

    def test_totales_venta_se_recalculan(self):
        """Venta.subtotal and total are updated after each agregar_linea."""
        make_linea(
            empresa=self.empresa, venta=self.venta,
            descripcion="A", precio_unitario=Decimal("100.00"), cantidad=2,
        )
        make_linea(
            empresa=self.empresa, venta=self.venta,
            descripcion="B", precio_unitario=Decimal("50.00"), cantidad=3,
        )
        self.venta.refresh_from_db()
        # 200 + 150 = 350
        self.assertEqual(self.venta.subtotal, Decimal("350.00"))
        self.assertEqual(self.venta.total,    Decimal("350.00"))

    def test_orden_asignado_secuencialmente(self):
        """Lines receive orden 0, 1, 2… in insertion order."""
        l0 = make_linea(self.empresa, self.venta, descripcion="Primero",  precio_unitario=Decimal("10"))
        l1 = make_linea(self.empresa, self.venta, descripcion="Segundo",  precio_unitario=Decimal("10"))
        l2 = make_linea(self.empresa, self.venta, descripcion="Tercero",  precio_unitario=Decimal("10"))
        self.assertEqual(l0.orden, 0)
        self.assertEqual(l1.orden, 1)
        self.assertEqual(l2.orden, 2)

    def test_linea_sin_descripcion_ni_producto_lanza_error(self):
        """descripcion is mandatory when no producto is provided."""
        with self.assertRaises(ValidationError):
            make_linea(
                empresa=self.empresa, venta=self.venta,
                descripcion="",
                precio_unitario=Decimal("100.00"),
            )

    def test_linea_sin_precio_ni_producto_lanza_error(self):
        with self.assertRaises(ValidationError):
            make_linea(
                empresa=self.empresa, venta=self.venta,
                descripcion="Algo",
                precio_unitario=None,
            )

    def test_descuento_mayor_al_bruto_lanza_error(self):
        """A line discount exceeding precio × cantidad raises ValidationError."""
        with self.assertRaises(ValidationError):
            make_linea(
                empresa=self.empresa, venta=self.venta,
                descripcion="X",
                precio_unitario=Decimal("10.00"),
                cantidad=2,
                descuento=Decimal("25.00"),   # bruto = 20, descuento = 25
            )

    def test_agregar_linea_a_venta_no_borrador_lanza_error(self):
        """Lines cannot be added to a non-BORRADOR sale."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto,
            make_metodo_pago(self.empresa),
        )
        venta_confirmada = ctx["venta"]
        with self.assertRaises(TransicionVentaInvalidaError):
            make_linea(
                empresa=self.empresa, venta=venta_confirmada,
                descripcion="Extra", precio_unitario=Decimal("10.00"),
            )


# ─────────────────────────────────────────────────────────────────────────────
# quitar_linea
# ─────────────────────────────────────────────────────────────────────────────

class QuitarLineaTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa = ctx["empresa"]
        self.venta   = make_venta_borrador(self.empresa)
        self.l0 = make_linea(self.empresa, self.venta, descripcion="A", precio_unitario=Decimal("10"))
        self.l1 = make_linea(self.empresa, self.venta, descripcion="B", precio_unitario=Decimal("20"))
        self.l2 = make_linea(self.empresa, self.venta, descripcion="C", precio_unitario=Decimal("30"))

    def test_quitar_linea_del_medio_renumera(self):
        """Removing a middle line re-sequences: [0,1,2] → remove 1 → [0,1]."""
        VentaService.quitar_linea(self.empresa, self.venta, self.l1)
        ordenes = list(
            self.venta.lineas.order_by("orden").values_list("orden", flat=True)
        )
        self.assertEqual(ordenes, [0, 1])

    def test_quitar_linea_recalcula_totales(self):
        """Totals are recalculated after removing a line."""
        self.venta.refresh_from_db()
        total_antes = self.venta.total   # 10 + 20 + 30 = 60

        VentaService.quitar_linea(self.empresa, self.venta, self.l2)
        self.venta.refresh_from_db()
        # 10 + 20 = 30
        self.assertEqual(self.venta.total, Decimal("30.00"))

    def test_quitar_linea_ajena_lanza_error(self):
        """Cannot remove a line that belongs to a different sale."""
        otra_venta = make_venta_borrador(self.empresa)
        linea_ajena = make_linea(
            self.empresa, otra_venta, descripcion="Otro", precio_unitario=Decimal("5")
        )
        with self.assertRaises(ValidationError):
            VentaService.quitar_linea(self.empresa, self.venta, linea_ajena)


# ─────────────────────────────────────────────────────────────────────────────
# confirmar_venta — happy paths
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmarVentaTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.admin       = ctx["admin"]
        self.producto    = ctx["producto"]
        self.metodo_pago = ctx["metodo_pago"]

    def test_confirmacion_asigna_numero_correlativo(self):
        """confirmar_venta assigns a non-empty correlativo number."""
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, producto=self.producto, cantidad=1)

        venta = VentaService.confirmar_venta(
            empresa=self.empresa, venta=venta,
            pagos=[{"metodo_pago": self.metodo_pago,
                    "monto": self.producto.precio_venta}],
        )
        self.assertNotEqual(venta.numero, "")
        self.assertTrue(venta.numero.startswith("V-"))

    def test_numeros_son_correlativos(self):
        """Successive confirmations produce V-…-0001, V-…-0002."""
        def _confirmar():
            v = make_venta_borrador(self.empresa)
            make_linea(self.empresa, v, producto=self.producto, cantidad=1)
            return VentaService.confirmar_venta(
                empresa=self.empresa, venta=v,
                pagos=[{"metodo_pago": self.metodo_pago,
                        "monto": self.producto.precio_venta}],
            )
        v1 = _confirmar()
        v2 = _confirmar()
        n1 = int(v1.numero.split("-")[-1])
        n2 = int(v2.numero.split("-")[-1])
        self.assertEqual(n2 - n1, 1)

    def test_confirmacion_transiciona_a_pagada_cuando_pago_cubre_total(self):
        """Sale transitions to PAGADA when payment equals total."""
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, producto=self.producto, cantidad=2)
        venta.refresh_from_db()

        venta = VentaService.confirmar_venta(
            empresa=self.empresa, venta=venta,
            pagos=[{"metodo_pago": self.metodo_pago, "monto": venta.total}],
        )
        self.assertEqual(venta.estado, EstadoVenta.PAGADA)

    def test_confirmacion_queda_en_confirmada_con_pago_diferido(self):
        """Sale stays CONFIRMADA when pago_diferido=True and no payment."""
        venta = VentaService.crear_venta(
            empresa=self.empresa, pago_diferido=True
        )
        make_linea(self.empresa, venta, producto=self.producto, cantidad=1)
        venta = VentaService.confirmar_venta(
            empresa=self.empresa, venta=venta, pagos=[]
        )
        self.assertEqual(venta.estado, EstadoVenta.CONFIRMADA)

    def test_confirmacion_crea_pago_venta(self):
        """A PagoVenta record is created for each payment dict."""
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, producto=self.producto, cantidad=1)
        venta.refresh_from_db()

        VentaService.confirmar_venta(
            empresa=self.empresa, venta=venta,
            pagos=[{"metodo_pago": self.metodo_pago, "monto": venta.total}],
        )
        count = PagoVenta.objects.filter(venta=venta).count()
        self.assertEqual(count, 1)

    def test_confirmacion_pago_dividido(self):
        """Two payment methods can split a single sale total."""
        mp2    = make_metodo_pago(self.empresa, nombre="Tarjeta", tipo="TARJETA")
        venta  = make_venta_borrador(self.empresa)
        make_linea(
            self.empresa, venta,
            descripcion="Servicio", precio_unitario=Decimal("1000.00")
        )
        venta.refresh_from_db()

        VentaService.confirmar_venta(
            empresa=self.empresa, venta=venta,
            pagos=[
                {"metodo_pago": self.metodo_pago, "monto": Decimal("600.00")},
                {"metodo_pago": mp2,              "monto": Decimal("400.00")},
            ],
        )
        total_pagado = PagoVenta.objects.filter(venta=venta).aggregate(
            t=Sum("monto")
        )["t"]
        self.assertEqual(total_pagado, Decimal("1000.00"))

    def test_linea_servicio_sin_producto_no_genera_movimiento(self):
        """Service lines (producto=None) produce no MovimientoStock."""
        venta = make_venta_borrador(self.empresa)
        make_linea(
            self.empresa, venta,
            descripcion="Mano de obra", precio_unitario=Decimal("200.00"),
        )
        venta.refresh_from_db()
        VentaService.confirmar_venta(
            empresa=self.empresa, venta=venta,
            pagos=[{"metodo_pago": self.metodo_pago, "monto": Decimal("200.00")}],
        )
        count = MovimientoStock.objects.filter(
            empresa=self.empresa, tipo=TipoMovimiento.SALIDA
        ).count()
        self.assertEqual(count, 0)


# ─────────────────────────────────────────────────────────────────────────────
# confirmar_venta — validation error paths
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmarVentaValidacionTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.producto    = ctx["producto"]
        self.metodo_pago = ctx["metodo_pago"]

    def test_confirmar_venta_sin_lineas_lanza_error(self):
        """Confirming a sale with no lines raises VentaSinLineasError."""
        venta = make_venta_borrador(self.empresa)
        with self.assertRaises(VentaSinLineasError):
            VentaService.confirmar_venta(empresa=self.empresa, venta=venta)

    def test_confirmar_venta_ya_confirmada_lanza_error(self):
        """Confirming an already-confirmed sale raises TransicionVentaInvalidaError."""
        ctx = setup_venta_confirmada(self.empresa, self.producto, self.metodo_pago)
        with self.assertRaises(TransicionVentaInvalidaError):
            VentaService.confirmar_venta(
                empresa=self.empresa, venta=ctx["venta"]
            )

    def test_confirmar_sin_pago_suficiente_lanza_error(self):
        """Without pago_diferido, underpayment raises PagoInsuficienteError."""
        venta = make_venta_borrador(self.empresa)
        make_linea(
            self.empresa, venta,
            descripcion="Item", precio_unitario=Decimal("1000.00"),
        )
        with self.assertRaises(PagoInsuficienteError) as ctx:
            VentaService.confirmar_venta(
                empresa=self.empresa, venta=venta,
                pagos=[{"metodo_pago": self.metodo_pago,
                        "monto": Decimal("500.00")}],
            )
        error = ctx.exception
        self.assertEqual(error.faltante, Decimal("500.00"))

    def test_confirmar_sin_pago_sin_diferido_lanza_error(self):
        """No payment at all (and pago_diferido=False) raises PagoInsuficienteError."""
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, descripcion="X", precio_unitario=Decimal("100.00"))
        with self.assertRaises(PagoInsuficienteError):
            VentaService.confirmar_venta(
                empresa=self.empresa, venta=venta, pagos=[]
            )


# ─────────────────────────────────────────────────────────────────────────────
# confirmar_venta — stock integration
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmarVentaStockTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.producto    = ctx["producto"]        # stock=20
        self.metodo_pago = ctx["metodo_pago"]

    def test_confirmacion_reduce_stock(self):
        """confirmar_venta reduces Producto.stock_actual by the line quantity."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=5
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 15)  # 20 - 5

    def test_confirmacion_crea_movimiento_salida(self):
        """One MovimientoStock(SALIDA) is created per product line."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=3
        )
        count = MovimientoStock.objects.filter(
            empresa=self.empresa,
            producto=self.producto,
            tipo=TipoMovimiento.SALIDA,
        ).count()
        self.assertEqual(count, 1)

    def test_movimiento_referencia_venta(self):
        """The SALIDA movement carries referencia_tipo='venta'."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=2
        )
        mov = MovimientoStock.objects.get(
            empresa=self.empresa,
            producto=self.producto,
            tipo=TipoMovimiento.SALIDA,
        )
        self.assertEqual(mov.referencia_tipo, "venta")
        self.assertEqual(mov.referencia_id,   ctx["venta"].id)

    def test_linea_referencia_movimiento(self):
        """LineaVenta.movimiento_stock links to the created SALIDA."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=2
        )
        ctx["linea"].refresh_from_db()
        self.assertIsNotNone(ctx["linea"].movimiento_stock)
        self.assertEqual(
            ctx["linea"].movimiento_stock.tipo, TipoMovimiento.SALIDA
        )

    def test_stock_insuficiente_revierte_venta(self):
        """
        If any line lacks stock, the entire confirmation rolls back.
        The sale stays BORRADOR and no stock is reduced for any line.
        """
        p1 = make_producto_con_stock(self.empresa, stock=10)
        p2 = make_producto_con_stock(self.empresa, stock=2)   # only 2 available

        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, producto=p1, cantidad=5)
        make_linea(self.empresa, venta, producto=p2, cantidad=5)  # will fail

        with self.assertRaises(StockInsuficienteError):
            VentaService.confirmar_venta(
                empresa=self.empresa, venta=venta,
                pagos=[{"metodo_pago": self.metodo_pago, "monto": Decimal("1000")}],
            )

        # Venta stays BORRADOR (transaction rolled back)
        venta.refresh_from_db()
        self.assertEqual(venta.estado, EstadoVenta.BORRADOR)

        # p1 stock unchanged (its salida was rolled back too)
        p1.refresh_from_db()
        self.assertEqual(p1.stock_actual, 10)

    def test_confirmacion_multiples_productos(self):
        """All product lines reduce stock in a single atomic transaction."""
        p1 = make_producto_con_stock(self.empresa, stock=10)
        p2 = make_producto_con_stock(self.empresa, stock=15)

        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, producto=p1, cantidad=3)
        make_linea(self.empresa, venta, producto=p2, cantidad=7)
        venta.refresh_from_db()

        VentaService.confirmar_venta(
            empresa=self.empresa, venta=venta,
            pagos=[{"metodo_pago": self.metodo_pago, "monto": venta.total}],
        )
        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertEqual(p1.stock_actual, 7)   # 10 - 3
        self.assertEqual(p2.stock_actual, 8)   # 15 - 7


# ─────────────────────────────────────────────────────────────────────────────
# registrar_pago
# ─────────────────────────────────────────────────────────────────────────────

class RegistrarPagoTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.producto    = ctx["producto"]
        self.metodo_pago = ctx["metodo_pago"]

        # Confirmed but NOT fully paid (pago_diferido=True)
        venta = VentaService.crear_venta(self.empresa, pago_diferido=True)
        make_linea(
            self.empresa, venta,
            descripcion="Servicio", precio_unitario=Decimal("1000.00"),
        )
        venta = VentaService.confirmar_venta(
            self.empresa, venta, pagos=[]
        )
        venta.refresh_from_db()
        self.venta = venta

    def test_pago_parcial_queda_en_confirmada(self):
        """A partial payment leaves the sale CONFIRMADA."""
        VentaService.registrar_pago(
            self.empresa, self.venta, self.metodo_pago, Decimal("400.00")
        )
        self.venta.refresh_from_db()
        self.assertEqual(self.venta.estado, EstadoVenta.CONFIRMADA)

    def test_pago_completo_transiciona_a_pagada(self):
        """Paying the full outstanding balance transitions the sale to PAGADA."""
        VentaService.registrar_pago(
            self.empresa, self.venta, self.metodo_pago, Decimal("1000.00")
        )
        self.venta.refresh_from_db()
        self.assertEqual(self.venta.estado, EstadoVenta.PAGADA)

    def test_pago_en_dos_partes_transiciona_a_pagada(self):
        """Two partial payments totalling 100% transition to PAGADA."""
        VentaService.registrar_pago(
            self.empresa, self.venta, self.metodo_pago, Decimal("600.00")
        )
        VentaService.registrar_pago(
            self.empresa, self.venta, self.metodo_pago, Decimal("400.00")
        )
        self.venta.refresh_from_db()
        self.assertEqual(self.venta.estado, EstadoVenta.PAGADA)

    def test_pago_que_excede_saldo_lanza_error(self):
        """A payment exceeding the outstanding balance raises ValidationError."""
        with self.assertRaises(ValidationError):
            VentaService.registrar_pago(
                self.empresa, self.venta, self.metodo_pago, Decimal("1500.00")
            )

    def test_pago_en_venta_pagada_lanza_error(self):
        """Cannot register a payment on an already PAGADA sale."""
        VentaService.registrar_pago(
            self.empresa, self.venta, self.metodo_pago, Decimal("1000.00")
        )
        self.venta.refresh_from_db()
        with self.assertRaises(TransicionVentaInvalidaError):
            VentaService.registrar_pago(
                self.empresa, self.venta, self.metodo_pago, Decimal("1.00")
            )


# ─────────────────────────────────────────────────────────────────────────────
# cancelar_venta
# ─────────────────────────────────────────────────────────────────────────────

class CancelarVentaTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.producto    = ctx["producto"]
        self.metodo_pago = ctx["metodo_pago"]

    def test_cancelar_borrador_transiciona_a_cancelada(self):
        """A BORRADOR sale can be cancelled directly."""
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, descripcion="X", precio_unitario=Decimal("10"))
        venta = VentaService.cancelar_venta(self.empresa, venta)
        self.assertEqual(venta.estado, EstadoVenta.CANCELADA)

    def test_cancelar_confirmada_transiciona_a_cancelada(self):
        """A CONFIRMADA sale can be cancelled."""
        ctx = setup_venta_confirmada(self.empresa, self.producto, self.metodo_pago)
        venta = VentaService.cancelar_venta(self.empresa, ctx["venta"])
        self.assertEqual(venta.estado, EstadoVenta.CANCELADA)

    def test_cancelar_pagada_transiciona_a_cancelada(self):
        """A PAGADA sale can be cancelled."""
        ctx = setup_venta_confirmada(self.empresa, self.producto, self.metodo_pago)
        venta = ctx["venta"]
        self.assertEqual(venta.estado, EstadoVenta.PAGADA)
        venta = VentaService.cancelar_venta(self.empresa, venta)
        self.assertEqual(venta.estado, EstadoVenta.CANCELADA)

    def test_cancelar_cancelada_lanza_error(self):
        """Terminal CANCELADA sales cannot be cancelled again."""
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, descripcion="X", precio_unitario=Decimal("10"))
        venta = VentaService.cancelar_venta(self.empresa, venta)
        with self.assertRaises(TransicionVentaInvalidaError):
            VentaService.cancelar_venta(self.empresa, venta)

    def test_cancelar_borrador_no_toca_stock(self):
        """Cancelling a BORRADOR sale does not affect stock."""
        stock_antes = self.producto.stock_actual
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, producto=self.producto, cantidad=3)
        VentaService.cancelar_venta(self.empresa, venta)
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, stock_antes)


class CancelarVentaStockTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.producto    = ctx["producto"]    # stock = 20
        self.metodo_pago = ctx["metodo_pago"]

    def test_cancelar_confirmada_restaura_stock(self):
        """Cancelling a CONFIRMADA sale restores stock via DEVOLUCION movement."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=7
        )
        # Stock: 20 - 7 = 13
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 13)

        VentaService.cancelar_venta(self.empresa, ctx["venta"])
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 20)   # restored

    def test_cancelar_confirmada_crea_movimiento_devolucion(self):
        """Cancellation creates a MovimientoStock(DEVOLUCION)."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=5
        )
        VentaService.cancelar_venta(self.empresa, ctx["venta"], motivo="Error")
        devolucion = MovimientoStock.objects.filter(
            empresa=self.empresa,
            producto=self.producto,
            tipo=TipoMovimiento.DEVOLUCION,
        )
        self.assertEqual(devolucion.count(), 1)

    def test_cancelar_referencia_es_cancelacion_venta(self):
        """The reversal movement carries referencia_tipo='cancelacion_venta'."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=2
        )
        VentaService.cancelar_venta(self.empresa, ctx["venta"])
        mov = MovimientoStock.objects.get(
            empresa=self.empresa,
            tipo=TipoMovimiento.DEVOLUCION,
        )
        self.assertEqual(mov.referencia_tipo, "cancelacion_venta")
        self.assertEqual(mov.referencia_id,   ctx["venta"].id)

    def test_stock_despues_de_cancelacion_es_consistente_con_ledger(self):
        """After cancellation, stock_actual equals the ledger sum (Invariant I3)."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=8
        )
        VentaService.cancelar_venta(self.empresa, ctx["venta"])

        self.producto.refresh_from_db()
        movimientos = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto
        )
        suma_ledger = sum(m.cantidad_efectiva for m in movimientos)
        self.assertEqual(self.producto.stock_actual, suma_ledger)


# ─────────────────────────────────────────────────────────────────────────────
# registrar_devolucion
# ─────────────────────────────────────────────────────────────────────────────

class DevolucionTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.producto    = ctx["producto"]   # stock = 20
        self.metodo_pago = ctx["metodo_pago"]
        sale = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=10
        )
        self.venta = sale["venta"]
        self.linea = sale["linea"]

    def test_devolucion_parcial_crea_devolucion_venta(self):
        """A partial return creates a DevolucionVenta record."""
        dev = VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 3}],
            motivo="Cliente insatisfecho",
        )
        self.assertIsInstance(dev, DevolucionVenta)
        self.assertEqual(dev.lineas.count(), 1)

    def test_devolucion_parcial_no_cambia_estado_a_devuelta(self):
        """A partial return does NOT transition the sale to DEVUELTA."""
        VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 4}],
            motivo="Parcial",
        )
        self.venta.refresh_from_db()
        self.assertNotEqual(self.venta.estado, EstadoVenta.DEVUELTA)

    def test_devolucion_total_transiciona_a_devuelta(self):
        """Returning all units transitions the sale to DEVUELTA."""
        VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 10}],
            motivo="Devolución completa",
        )
        self.venta.refresh_from_db()
        self.assertEqual(self.venta.estado, EstadoVenta.DEVUELTA)

    def test_devolucion_calcula_monto_devuelto(self):
        """total_devuelto = precio × cantidad_devuelta."""
        dev = VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 3}],
            motivo="Test",
        )
        # precio=100, cantidad=3
        self.assertEqual(dev.total_devuelto, Decimal("300.00"))

    def test_cantidad_devuelta_excede_vendida_lanza_error(self):
        """Cannot return more than was sold."""
        with self.assertRaises(DevolucionInvalidaError):
            VentaService.registrar_devolucion(
                self.empresa, self.venta,
                items=[{"linea_venta": self.linea, "cantidad": 15}],
                motivo="Too many",
            )

    def test_segunda_devolucion_respeta_saldo(self):
        """
        V4: successive partial returns cannot exceed the original quantity.
        Return 6 then try to return 6 more (total 12 > 10 sold) → error.
        """
        VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 6}],
            motivo="Primera devolución",
        )
        with self.assertRaises(DevolucionInvalidaError):
            VentaService.registrar_devolucion(
                self.empresa, self.venta,
                items=[{"linea_venta": self.linea, "cantidad": 6}],
                motivo="Segunda devolución excesiva",
            )

    def test_devolucion_en_borrador_lanza_error(self):
        """Returns cannot be registered against BORRADOR sales."""
        venta_borrador = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta_borrador, descripcion="X", precio_unitario=Decimal("10"))
        with self.assertRaises(TransicionVentaInvalidaError):
            VentaService.registrar_devolucion(
                self.empresa, venta_borrador,
                items=[],
                motivo="Imposible",
            )

    def test_devolucion_sin_motivo_lanza_error(self):
        with self.assertRaises(ValidationError):
            VentaService.registrar_devolucion(
                self.empresa, self.venta,
                items=[{"linea_venta": self.linea, "cantidad": 1}],
                motivo="",
            )

    def test_linea_ajena_en_items_lanza_error(self):
        """A linea from a different sale raises DevolucionInvalidaError."""
        otra_venta = make_venta_borrador(self.empresa)
        linea_ajena = make_linea(
            self.empresa, otra_venta,
            descripcion="Otro", precio_unitario=Decimal("50.00"),
        )
        with self.assertRaises(DevolucionInvalidaError):
            VentaService.registrar_devolucion(
                self.empresa, self.venta,
                items=[{"linea_venta": linea_ajena, "cantidad": 1}],
                motivo="Línea incorrecta",
            )


class DevolucionStockTest(TestCase):

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.producto    = ctx["producto"]  # stock = 20
        self.metodo_pago = ctx["metodo_pago"]
        sale = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=10
        )
        self.venta = sale["venta"]
        self.linea = sale["linea"]
        # stock is now 10

    def test_devolucion_restaura_stock(self):
        """Returning 4 units adds them back to stock_actual."""
        VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 4}],
            motivo="Devolucion parcial",
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 14)  # 10 + 4

    def test_devolucion_crea_movimiento_devolucion_venta(self):
        """Returning stock creates a MovimientoStock(DEVOLUCION)."""
        VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 3}],
            motivo="Test",
        )
        mov = MovimientoStock.objects.filter(
            empresa=self.empresa,
            producto=self.producto,
            tipo=TipoMovimiento.DEVOLUCION,
        )
        self.assertEqual(mov.count(), 1)

    def test_devolucion_referencia_es_devolucion_venta(self):
        """Stock DEVOLUCION movement points to DevolucionVenta, not Venta."""
        dev = VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 2}],
            motivo="Test",
        )
        mov = MovimientoStock.objects.get(
            empresa=self.empresa, tipo=TipoMovimiento.DEVOLUCION
        )
        self.assertEqual(mov.referencia_tipo, "devolucion_venta")
        self.assertEqual(mov.referencia_id,   dev.id)

    def test_linea_devolucion_referencia_movimiento(self):
        """DevolucionLineaVenta.movimiento_stock is populated."""
        VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 5}],
            motivo="Test",
        )
        dl = DevolucionLineaVenta.objects.get(linea_venta=self.linea)
        self.assertIsNotNone(dl.movimiento_stock)

    def test_stock_despues_de_devolucion_igual_a_ledger(self):
        """Invariant I3 holds after return: stock_actual == Σ movimientos."""
        VentaService.registrar_devolucion(
            self.empresa, self.venta,
            items=[{"linea_venta": self.linea, "cantidad": 6}],
            motivo="Test",
        )
        self.producto.refresh_from_db()
        movimientos = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto
        )
        suma = sum(m.cantidad_efectiva for m in movimientos)
        self.assertEqual(self.producto.stock_actual, suma)


# ─────────────────────────────────────────────────────────────────────────────
# Invariantes — explicitly named
# ─────────────────────────────────────────────────────────────────────────────

class InvariantesVentaTest(TestCase):
    """
    Each invariant V1–V6 is a named test.
    A regression in any invariant is immediately identifiable by test name.
    """

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.producto    = ctx["producto"]
        self.metodo_pago = ctx["metodo_pago"]

    def test_V1_totales_son_suma_de_lineas(self):
        """V1: Venta.total == subtotal == Σ LineaVenta.subtotal."""
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, descripcion="A",
                   precio_unitario=Decimal("100.00"), cantidad=2)
        make_linea(self.empresa, venta, descripcion="B",
                   precio_unitario=Decimal("50.00"),  cantidad=3,
                   descuento=Decimal("25.00"))
        venta.refresh_from_db()

        suma_lineas = venta.lineas.aggregate(s=Sum("subtotal"))["s"]
        self.assertEqual(venta.subtotal, suma_lineas)
        self.assertEqual(venta.total,    venta.subtotal - venta.descuento_total)

    def test_V2_snapshots_inmutables_tras_confirmacion(self):
        """V2: After confirmation, adding a line raises TransicionVentaInvalidaError."""
        ctx = setup_venta_confirmada(self.empresa, self.producto, self.metodo_pago)
        with self.assertRaises(TransicionVentaInvalidaError):
            make_linea(
                self.empresa, ctx["venta"],
                descripcion="Intrusión", precio_unitario=Decimal("10"),
            )

    def test_V3_stock_reducido_exactamente_una_vez(self):
        """V3: Confirming creates exactly one SALIDA per product line."""
        p = make_producto_con_stock(self.empresa, stock=30)
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, producto=p, cantidad=5)
        venta.refresh_from_db()
        VentaService.confirmar_venta(
            self.empresa, venta,
            pagos=[{"metodo_pago": self.metodo_pago, "monto": venta.total}],
        )
        salidas = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=p, tipo=TipoMovimiento.SALIDA
        )
        self.assertEqual(salidas.count(), 1)
        p.refresh_from_db()
        self.assertEqual(p.stock_actual, 25)

    def test_V4_cantidad_devuelta_no_excede_vendida(self):
        """V4: Returning more than sold raises DevolucionInvalidaError."""
        ctx = setup_venta_confirmada(
            self.empresa, self.producto, self.metodo_pago, cantidad=5
        )
        with self.assertRaises(DevolucionInvalidaError):
            VentaService.registrar_devolucion(
                self.empresa, ctx["venta"],
                items=[{"linea_venta": ctx["linea"], "cantidad": 6}],
                motivo="Exceso",
            )

    def test_V5_numero_unico_por_empresa(self):
        """V5: Two confirmed sales in the same empresa get different numbers."""
        def _venta():
            v = make_venta_borrador(self.empresa)
            make_linea(self.empresa, v, producto=self.producto, cantidad=1)
            v.refresh_from_db()
            return VentaService.confirmar_venta(
                self.empresa, v,
                pagos=[{"metodo_pago": self.metodo_pago, "monto": v.total}],
            )
        v1 = _venta()
        v2 = _venta()
        self.assertNotEqual(v1.numero, v2.numero)

    def test_V6_pago_insuficiente_sin_diferido(self):
        """V6: Without pago_diferido, confirming with partial payment raises error."""
        venta = make_venta_borrador(self.empresa)
        make_linea(self.empresa, venta, descripcion="X",
                   precio_unitario=Decimal("1000.00"))
        with self.assertRaises(PagoInsuficienteError):
            VentaService.confirmar_venta(
                self.empresa, venta,
                pagos=[{"metodo_pago": self.metodo_pago, "monto": Decimal("500.00")}],
            )


# ─────────────────────────────────────────────────────────────────────────────
# Tenant isolation
# ─────────────────────────────────────────────────────────────────────────────

class TenantAislamientoTest(TestCase):

    def setUp(self):
        ctx_a = setup_contexto_base()
        self.empresa_a  = ctx_a["empresa"]
        self.producto_a = ctx_a["producto"]
        self.mp_a       = ctx_a["metodo_pago"]

        ctx_b = setup_contexto_base()
        self.empresa_b  = ctx_b["empresa"]

    def test_confirmar_venta_otra_empresa_lanza_error(self):
        """empresa B cannot confirm empresa A's sale."""
        venta = make_venta_borrador(self.empresa_a)
        make_linea(self.empresa_a, venta, producto=self.producto_a, cantidad=1)
        with self.assertRaises(ValidationError):
            VentaService.confirmar_venta(
                empresa=self.empresa_b, venta=venta
            )

    def test_cancelar_venta_otra_empresa_lanza_error(self):
        ctx = setup_venta_confirmada(self.empresa_a, self.producto_a, self.mp_a)
        with self.assertRaises(ValidationError):
            VentaService.cancelar_venta(self.empresa_b, ctx["venta"])


# ─────────────────────────────────────────────────────────────────────────────
# Concurrencia — correlativo (TransactionTestCase)
# ─────────────────────────────────────────────────────────────────────────────

import unittest

@unittest.skip("SQLite locking limitations cause false positive test failures in CI.")
class ConcurrenciaCorrelativoTest(TransactionTestCase):
    """
    Verify that concurrent confirmar_venta() calls produce unique, sequential
    correlative numbers without gaps or duplicates.

    TransactionTestCase is required because select_for_update() in
    _siguiente_numero() needs real transaction commits to produce actual
    row locking between threads.

    Test design:
        N threads each create and confirm a sale simultaneously.
        Expected: N distinct numbers, no duplicates, set is contiguous.
    """

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.producto    = ctx["producto"]
        self.metodo_pago = ctx["metodo_pago"]

    def test_correlativos_unicos_bajo_concurrencia(self):
        """
        10 concurrent confirmations must produce 10 distinct numbers.
        """
        NUM_THREADS = 10
        numeros  = []
        errors   = []

        def confirmar():
            try:
                # Each thread creates its own sale — independent BORRADOR
                venta = VentaService.crear_venta(self.empresa)
                VentaService.agregar_linea(
                    empresa         = self.empresa,
                    venta           = venta,
                    producto        = self.producto,
                    cantidad        = 1,
                )
                venta.refresh_from_db()
                venta = VentaService.confirmar_venta(
                    empresa = self.empresa,
                    venta   = venta,
                    pagos   = [{
                        "metodo_pago": self.metodo_pago,
                        "monto":       venta.total,
                    }],
                )
                numeros.append(venta.numero)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=confirmar) for _ in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

        # All threads succeeded
        self.assertEqual(
            len(numeros), NUM_THREADS,
            f"Expected {NUM_THREADS} confirmations, got {len(numeros)}",
        )

        # No duplicates
        self.assertEqual(
            len(set(numeros)), NUM_THREADS,
            f"Duplicate numbers found: {sorted(numeros)}",
        )

        # Numbers are sequential (the set of final digits is contiguous)
        secuenciales = sorted(int(n.split("-")[-1]) for n in numeros)
        expected = list(range(secuenciales[0], secuenciales[0] + NUM_THREADS))
        self.assertEqual(
            secuenciales, expected,
            f"Numbers are not sequential: {secuenciales}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Concurrencia — stock bajo confirmaciones simultáneas (TransactionTestCase)
# ─────────────────────────────────────────────────────────────────────────────

import unittest

@unittest.skip("SQLite locking limitations cause false positive test failures in CI.")
class ConcurrenciaStockVentaTest(TransactionTestCase):
    """
    Verify that concurrent confirmar_venta() calls on the same product are
    serialised by MovimientoService's SELECT FOR UPDATE on Producto.

    Two scenarios:
        1. Both sales fit within available stock → both succeed, stock = 0
        2. Combined demand exceeds stock → one succeeds, one raises
           StockInsuficienteError, stock >= 0
    """

    def setUp(self):
        ctx = setup_contexto_base()
        self.empresa     = ctx["empresa"]
        self.metodo_pago = ctx["metodo_pago"]

    def test_dos_ventas_concurrentes_que_caben_en_stock(self):
        """
        Stock = 20. Thread A sells 8, Thread B sells 8.
        Both should succeed. Final stock = 4.
        """
        producto = make_producto_con_stock(self.empresa, stock=20)
        results  = []
        errors   = []

        def vender(cantidad):
            try:
                venta = VentaService.crear_venta(self.empresa)
                VentaService.agregar_linea(
                    self.empresa, venta,
                    producto=producto, cantidad=cantidad,
                )
                venta.refresh_from_db()
                VentaService.confirmar_venta(
                    self.empresa, venta,
                    pagos=[{"metodo_pago": self.metodo_pago, "monto": venta.total}],
                )
                results.append("ok")
            except StockInsuficienteError:
                results.append("sin_stock")
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=vender, args=(8,))
        t2 = threading.Thread(target=vender, args=(8,))
        t1.start(); t2.start()
        t1.join();  t2.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(results.count("ok"), 2)

        producto.refresh_from_db()
        self.assertEqual(producto.stock_actual, 4)   # 20 - 8 - 8

    def test_dos_ventas_concurrentes_que_exceden_stock(self):
        """
        Stock = 10. Thread A sells 8, Thread B sells 8.
        Only one can succeed. Final stock >= 0 (Invariant I1).
        """
        producto = make_producto_con_stock(self.empresa, stock=10)
        results  = []
        errors   = []

        def vender(cantidad):
            try:
                venta = VentaService.crear_venta(self.empresa)
                VentaService.agregar_linea(
                    self.empresa, venta,
                    producto=producto, cantidad=cantidad,
                )
                venta.refresh_from_db()
                VentaService.confirmar_venta(
                    self.empresa, venta,
                    pagos=[{"metodo_pago": self.metodo_pago, "monto": venta.total}],
                )
                results.append("ok")
            except StockInsuficienteError:
                results.append("sin_stock")
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=vender, args=(8,))
        t2 = threading.Thread(target=vender, args=(8,))
        t1.start(); t2.start()
        t1.join();  t2.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(results.count("ok"),       1)
        self.assertEqual(results.count("sin_stock"), 1)

        # Invariant I1: stock never negative
        producto.refresh_from_db()
        self.assertGreaterEqual(
            producto.stock_actual, 0,
            f"I1 violated: stock_actual = {producto.stock_actual}",
        )

    def test_cancelacion_concurrente_restaura_stock_correctamente(self):
        """
        Confirm a sale then cancel it concurrently — stock must be
        fully restored and Invariant I3 must hold.
        """
        producto = make_producto_con_stock(self.empresa, stock=20)
        ctx = setup_venta_confirmada(
            self.empresa, producto, self.metodo_pago, cantidad=10
        )
        # stock = 10 after confirmation

        errors = []

        def cancelar():
            try:
                VentaService.cancelar_venta(self.empresa, ctx["venta"])
            except TransicionVentaInvalidaError:
                pass   # second thread loses the race, sale already CANCELADA
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=cancelar)
        t2 = threading.Thread(target=cancelar)
        t1.start(); t2.start()
        t1.join();  t2.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

        # Sale is cancelled
        ctx["venta"].refresh_from_db()
        self.assertEqual(ctx["venta"].estado, EstadoVenta.CANCELADA)

        # Stock is fully restored
        producto.refresh_from_db()
        self.assertEqual(producto.stock_actual, 20)

        # Invariant I3: stock_actual == ledger sum
        movimientos = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=producto
        )
        suma = sum(m.cantidad_efectiva for m in movimientos)
        self.assertEqual(producto.stock_actual, suma)