"""
modules/inventario/tests/test_movimiento_service.py

Service-layer tests for MovimientoService.

Test structure:
    EntradaStockTest          → registrar_entrada
    SalidaStockTest           → registrar_salida (happy paths + StockInsuficiente)
    AjusteStockTest           → registrar_ajuste (positive, negative, edge cases)
    DevolucionTest            → registrar_devolucion
    MermaTest                 → registrar_merma
    InvariantesTest           → I1–I5 verified explicitly
    TenantAislamientoTest     → cross-tenant safety
    ConcurrenciaStockTest     → select_for_update under concurrent writes
                                (TransactionTestCase — real commits required)

Strategy:
    - Call service methods directly — no HTTP.
    - Assert on both the returned MovimientoStock AND the DB state of Producto.
    - The InvariantesTest class verifies invariants as explicit, named tests —
      not only as side effects of other tests.
    - The concurrency test uses threading.Thread to simulate two simultaneous
      requests against the same product.
"""

import threading
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase

from modules.inventario.exceptions import (
    AjusteInnecesarioError,
    ProductoInactivoError,
    StockInsuficienteError,
)
from modules.inventario.models import MovimientoStock, Producto, TipoMovimiento
from modules.inventario.services import MovimientoService
from modules.inventario.tests.factories import (
    make_admin,
    make_empresa,
    make_producto,
    setup_producto_con_stock,
)


# ─────────────────────────────────────────────────────────────────────────────
# registrar_entrada
# ─────────────────────────────────────────────────────────────────────────────

class EntradaStockTest(TestCase):

    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=0)
        self.empresa  = ctx["empresa"]
        self.admin    = ctx["admin"]
        self.producto = ctx["producto"]

    def test_entrada_incrementa_stock_actual(self):
        """registrar_entrada adds cantidad to Producto.stock_actual."""
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=10
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 10)

    def test_entrada_crea_movimiento_en_db(self):
        """One MovimientoStock record must be created."""
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=10
        )
        count = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto, tipo=TipoMovimiento.ENTRADA
        ).count()
        self.assertEqual(count, 1)

    def test_entrada_registra_snapshots_correctos(self):
        """stock_anterior and stock_resultante must be correct snapshots."""
        self.producto.stock_actual = 5
        self.producto.save(update_fields=["stock_actual"])

        mov = MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=10
        )

        self.assertEqual(mov.stock_anterior,   5)
        self.assertEqual(mov.stock_resultante, 15)

    def test_entrada_acumulada(self):
        """Multiple entradas accumulate correctly."""
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=10
        )
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=5
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 15)

    def test_entrada_registra_referencia(self):
        """referencia_tipo and referencia_id are stored on the movement."""
        import uuid
        ref_id = uuid.uuid4()
        mov = MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=10,
            referencia_tipo="orden_compra", referencia_id=ref_id,
        )
        self.assertEqual(mov.referencia_tipo, "orden_compra")
        self.assertEqual(mov.referencia_id,   ref_id)

    def test_entrada_registra_costo_unitario(self):
        """costo_unitario is stored for inventory valuation."""
        mov = MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=10,
            costo_unitario=Decimal("99.50"),
        )
        self.assertEqual(mov.costo_unitario, Decimal("99.50"))

    def test_entrada_registra_created_by(self):
        """created_by must be set to the provided usuario."""
        mov = MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto,
            cantidad=10, usuario=self.admin,
        )
        self.assertEqual(mov.created_by, self.admin)

    def test_entrada_cantidad_cero_lanza_error(self):
        """cantidad=0 violates Invariant I5 — must raise ValidationError."""
        with self.assertRaises(ValidationError):
            MovimientoService.registrar_entrada(
                empresa=self.empresa, producto=self.producto, cantidad=0
            )

    def test_entrada_cantidad_negativa_lanza_error(self):
        """Negative cantidad violates Invariant I5."""
        with self.assertRaises(ValidationError):
            MovimientoService.registrar_entrada(
                empresa=self.empresa, producto=self.producto, cantidad=-5
            )

    def test_entrada_producto_inactivo_lanza_error(self):
        """Operations on inactive products must raise ProductoInactivoError."""
        producto_inactivo = make_producto(self.empresa, activo=False)
        with self.assertRaises(ProductoInactivoError):
            MovimientoService.registrar_entrada(
                empresa=self.empresa, producto=producto_inactivo, cantidad=10
            )


# ─────────────────────────────────────────────────────────────────────────────
# registrar_salida
# ─────────────────────────────────────────────────────────────────────────────

class SalidaStockTest(TestCase):

    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=20)
        self.empresa  = ctx["empresa"]
        self.admin    = ctx["admin"]
        self.producto = ctx["producto"]

    def test_salida_decrementa_stock_actual(self):
        """registrar_salida subtracts cantidad from Producto.stock_actual."""
        MovimientoService.registrar_salida(
            empresa=self.empresa, producto=self.producto, cantidad=8
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 12)  # 20 - 8

    def test_salida_registra_snapshots_correctos(self):
        """stock_anterior=20, stock_resultante=12 for salida of 8."""
        mov = MovimientoService.registrar_salida(
            empresa=self.empresa, producto=self.producto, cantidad=8
        )
        self.assertEqual(mov.stock_anterior,   20)
        self.assertEqual(mov.stock_resultante, 12)
        self.assertEqual(mov.tipo,             TipoMovimiento.SALIDA)

    def test_salida_exacta_al_limite_ok(self):
        """Selling exactly stock_actual leaves stock at 0 — must succeed."""
        mov = MovimientoService.registrar_salida(
            empresa=self.empresa, producto=self.producto, cantidad=20
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 0)
        self.assertEqual(mov.stock_resultante, 0)

    def test_salida_sin_stock_lanza_error(self):
        """Selling more than available raises StockInsuficienteError."""
        with self.assertRaises(StockInsuficienteError) as ctx:
            MovimientoService.registrar_salida(
                empresa=self.empresa, producto=self.producto, cantidad=25
            )

        error = ctx.exception
        self.assertEqual(error.disponible, 20)
        self.assertEqual(error.solicitado, 25)

    def test_salida_sin_stock_no_crea_movimiento(self):
        """When StockInsuficienteError is raised, NO movement is created."""
        movimientos_antes = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto
        ).count()

        with self.assertRaises(StockInsuficienteError):
            MovimientoService.registrar_salida(
                empresa=self.empresa, producto=self.producto, cantidad=999
            )

        movimientos_despues = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto
        ).count()
        # The @transaction.atomic rollback ensures nothing was persisted
        self.assertEqual(movimientos_antes, movimientos_despues)

    def test_salida_sin_stock_no_modifica_stock_actual(self):
        """After a failed salida, stock_actual is unchanged."""
        with self.assertRaises(StockInsuficienteError):
            MovimientoService.registrar_salida(
                empresa=self.empresa, producto=self.producto, cantidad=999
            )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 20)

    def test_salida_con_permite_stock_negativo_ok(self):
        """Products with permite_stock_negativo=True can go below zero."""
        producto_neg = make_producto(self.empresa, stock_actual=5, permite_stock_negativo=True)
        mov = MovimientoService.registrar_salida(
            empresa=self.empresa, producto=producto_neg, cantidad=8
        )
        producto_neg.refresh_from_db()
        self.assertEqual(producto_neg.stock_actual, -3)
        self.assertEqual(mov.stock_resultante, -3)

    def test_salida_registra_referencia_venta(self):
        """referencia_tipo='venta' is stored for cross-module traceability."""
        import uuid
        venta_id = uuid.uuid4()
        mov = MovimientoService.registrar_salida(
            empresa=self.empresa, producto=self.producto, cantidad=5,
            referencia_tipo="venta", referencia_id=venta_id,
        )
        self.assertEqual(mov.referencia_tipo, "venta")
        self.assertEqual(mov.referencia_id,   venta_id)

    def test_salida_producto_inactivo_lanza_error(self):
        producto_inactivo = make_producto(self.empresa, activo=False, stock_actual=10)
        with self.assertRaises(ProductoInactivoError):
            MovimientoService.registrar_salida(
                empresa=self.empresa, producto=producto_inactivo, cantidad=5
            )


# ─────────────────────────────────────────────────────────────────────────────
# registrar_ajuste
# ─────────────────────────────────────────────────────────────────────────────

class AjusteStockTest(TestCase):

    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=20)
        self.empresa  = ctx["empresa"]
        self.admin    = ctx["admin"]
        self.producto = ctx["producto"]

    def test_ajuste_positivo_incrementa_stock(self):
        """stock_nuevo > stock_actual → AJUSTE_POSITIVO."""
        mov = MovimientoService.registrar_ajuste(
            empresa=self.empresa, producto=self.producto,
            stock_nuevo=30, motivo="Conteo físico: encontré 30 unidades",
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 30)
        self.assertEqual(mov.tipo,             TipoMovimiento.AJUSTE_POSITIVO)
        self.assertEqual(mov.cantidad,         10)    # delta = 30 - 20
        self.assertEqual(mov.stock_anterior,   20)
        self.assertEqual(mov.stock_resultante, 30)

    def test_ajuste_negativo_decrementa_stock(self):
        """stock_nuevo < stock_actual → AJUSTE_NEGATIVO."""
        mov = MovimientoService.registrar_ajuste(
            empresa=self.empresa, producto=self.producto,
            stock_nuevo=15, motivo="Faltante encontrado en conteo",
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 15)
        self.assertEqual(mov.tipo,             TipoMovimiento.AJUSTE_NEGATIVO)
        self.assertEqual(mov.cantidad,         5)     # delta = |15 - 20|
        self.assertEqual(mov.stock_anterior,   20)
        self.assertEqual(mov.stock_resultante, 15)

    def test_ajuste_al_mismo_valor_lanza_error(self):
        """Adjusting to the current value raises AjusteInnecesarioError."""
        with self.assertRaises(AjusteInnecesarioError) as ctx:
            MovimientoService.registrar_ajuste(
                empresa=self.empresa, producto=self.producto,
                stock_nuevo=20, motivo="Conteo confirma stock",
            )
        self.assertEqual(ctx.exception.stock_actual, 20)

    def test_ajuste_sin_motivo_lanza_error(self):
        """motivo is mandatory for adjustments — must raise ValidationError."""
        with self.assertRaises(ValidationError):
            MovimientoService.registrar_ajuste(
                empresa=self.empresa, producto=self.producto,
                stock_nuevo=15, motivo="",
            )

    def test_ajuste_motivo_solo_espacios_lanza_error(self):
        with self.assertRaises(ValidationError):
            MovimientoService.registrar_ajuste(
                empresa=self.empresa, producto=self.producto,
                stock_nuevo=15, motivo="   ",
            )

    def test_ajuste_negativo_a_cero_ok(self):
        """Adjusting to zero is valid for products without negative stock."""
        mov = MovimientoService.registrar_ajuste(
            empresa=self.empresa, producto=self.producto,
            stock_nuevo=0, motivo="Stock agotado, ajuste a cero",
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 0)

    def test_ajuste_a_negativo_sin_permiso_lanza_error(self):
        """Adjusting to a negative value raises StockInsuficienteError."""
        with self.assertRaises(StockInsuficienteError):
            MovimientoService.registrar_ajuste(
                empresa=self.empresa, producto=self.producto,
                stock_nuevo=-5, motivo="Intento de ajuste negativo",
            )

    def test_ajuste_a_negativo_con_permiso_ok(self):
        """Products with permite_stock_negativo=True can be adjusted below zero."""
        producto_neg = make_producto(self.empresa, stock_actual=5, permite_stock_negativo=True)
        mov = MovimientoService.registrar_ajuste(
            empresa=self.empresa, producto=producto_neg,
            stock_nuevo=-3, motivo="Ajuste de inventario especial",
        )
        producto_neg.refresh_from_db()
        self.assertEqual(producto_neg.stock_actual, -3)
        self.assertEqual(mov.tipo, TipoMovimiento.AJUSTE_NEGATIVO)

    def test_ajuste_referencia_tipo_es_ajuste_manual(self):
        """Adjustments are tagged with referencia_tipo='ajuste_manual'."""
        mov = MovimientoService.registrar_ajuste(
            empresa=self.empresa, producto=self.producto,
            stock_nuevo=18, motivo="Ajuste rutinario",
        )
        self.assertEqual(mov.referencia_tipo, "ajuste_manual")
        self.assertIsNone(mov.referencia_id)


# ─────────────────────────────────────────────────────────────────────────────
# registrar_devolucion
# ─────────────────────────────────────────────────────────────────────────────

class DevolucionTest(TestCase):

    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=10)
        self.empresa  = ctx["empresa"]
        self.admin    = ctx["admin"]
        self.producto = ctx["producto"]
        self.venta_id = __import__("uuid").uuid4()

    def test_devolucion_incrementa_stock(self):
        """A return restores stock_actual."""
        MovimientoService.registrar_devolucion(
            empresa=self.empresa, producto=self.producto, cantidad=3,
            referencia_tipo="venta", referencia_id=self.venta_id,
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 13)

    def test_devolucion_tipo_es_devolucion(self):
        """The movement type must be DEVOLUCION, not ENTRADA."""
        mov = MovimientoService.registrar_devolucion(
            empresa=self.empresa, producto=self.producto, cantidad=3,
            referencia_tipo="venta", referencia_id=self.venta_id,
        )
        self.assertEqual(mov.tipo, TipoMovimiento.DEVOLUCION)

    def test_devolucion_registra_referencia(self):
        """referencia_tipo and referencia_id must point to the original sale."""
        mov = MovimientoService.registrar_devolucion(
            empresa=self.empresa, producto=self.producto, cantidad=3,
            referencia_tipo="venta", referencia_id=self.venta_id,
        )
        self.assertEqual(mov.referencia_tipo, "venta")
        self.assertEqual(mov.referencia_id,   self.venta_id)

    def test_devolucion_sin_referencia_lanza_error(self):
        """A return without an origin reference must raise ValidationError."""
        with self.assertRaises(ValidationError) as ctx:
            MovimientoService.registrar_devolucion(
                empresa=self.empresa, producto=self.producto, cantidad=3,
                referencia_tipo="", referencia_id=None,
            )
        self.assertIn("referencia", str(ctx.exception).lower())

    def test_devolucion_sin_referencia_id_lanza_error(self):
        with self.assertRaises(ValidationError):
            MovimientoService.registrar_devolucion(
                empresa=self.empresa, producto=self.producto, cantidad=3,
                referencia_tipo="venta", referencia_id=None,
            )

    def test_devolucion_registra_costo_unitario(self):
        mov = MovimientoService.registrar_devolucion(
            empresa=self.empresa, producto=self.producto, cantidad=2,
            referencia_tipo="venta", referencia_id=self.venta_id,
            costo_unitario=Decimal("80.00"),
        )
        self.assertEqual(mov.costo_unitario, Decimal("80.00"))


# ─────────────────────────────────────────────────────────────────────────────
# registrar_merma
# ─────────────────────────────────────────────────────────────────────────────

class MermaTest(TestCase):

    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=20)
        self.empresa  = ctx["empresa"]
        self.admin    = ctx["admin"]
        self.producto = ctx["producto"]

    def test_merma_reduce_stock(self):
        """registrar_merma decrements stock_actual."""
        MovimientoService.registrar_merma(
            empresa=self.empresa, producto=self.producto,
            cantidad=3, motivo="Productos vencidos",
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 17)

    def test_merma_tipo_es_merma(self):
        """The movement type must be MERMA, not SALIDA."""
        mov = MovimientoService.registrar_merma(
            empresa=self.empresa, producto=self.producto,
            cantidad=3, motivo="Rotura durante traslado",
        )
        self.assertEqual(mov.tipo, TipoMovimiento.MERMA)

    def test_merma_sin_motivo_lanza_error(self):
        """motivo is mandatory for merma — must raise ValidationError."""
        with self.assertRaises(ValidationError):
            MovimientoService.registrar_merma(
                empresa=self.empresa, producto=self.producto,
                cantidad=3, motivo="",
            )

    def test_merma_excede_stock_lanza_error(self):
        """Merma exceeding stock raises StockInsuficienteError."""
        with self.assertRaises(StockInsuficienteError) as ctx:
            MovimientoService.registrar_merma(
                empresa=self.empresa, producto=self.producto,
                cantidad=25, motivo="Daño por inundación",
            )
        self.assertEqual(ctx.exception.disponible, 20)
        self.assertEqual(ctx.exception.solicitado, 25)

    def test_merma_con_permite_stock_negativo_ok(self):
        """Products with permite_stock_negativo=True allow merma below zero."""
        producto_neg = make_producto(self.empresa, stock_actual=5, permite_stock_negativo=True)
        mov = MovimientoService.registrar_merma(
            empresa=self.empresa, producto=producto_neg,
            cantidad=8, motivo="Pérdida total del lote",
        )
        producto_neg.refresh_from_db()
        self.assertEqual(producto_neg.stock_actual, -3)


# ─────────────────────────────────────────────────────────────────────────────
# Invariantes — explicitly verified as named tests
# ─────────────────────────────────────────────────────────────────────────────

class InvariantesTest(TestCase):
    """
    Explicit verification of the domain invariants I1–I5.

    Each invariant is a named test so any regression is immediately
    identifiable by test name — not hidden inside a larger test.
    """

    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=0)
        self.empresa  = ctx["empresa"]
        self.admin    = ctx["admin"]
        self.producto = ctx["producto"]

    def test_I1_stock_no_negativo_cuando_no_permitido(self):
        """I1: stock_actual must never go below 0 for standard products."""
        # Set stock to 5 via service
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=5
        )
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 5)

        # Try to reduce by more than available
        with self.assertRaises(StockInsuficienteError):
            MovimientoService.registrar_salida(
                empresa=self.empresa, producto=self.producto, cantidad=6
            )

        # Stock must be unchanged after failed operation
        self.producto.refresh_from_db()
        self.assertGreaterEqual(self.producto.stock_actual, 0)

    def test_I3_stock_actual_igual_a_suma_movimientos(self):
        """
        I3: stock_actual must equal the algebraic sum of all MovimientoStock.

        This test verifies that stock_actual (the denormalized cache) stays
        in sync with the ledger after a sequence of mixed operations.
        """
        # Sequence of operations: +20, -8, +5, -3
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=20
        )
        MovimientoService.registrar_salida(
            empresa=self.empresa, producto=self.producto, cantidad=8
        )
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=5
        )
        MovimientoService.registrar_salida(
            empresa=self.empresa, producto=self.producto, cantidad=3
        )
        # Expected: 20 - 8 + 5 - 3 = 14

        self.producto.refresh_from_db()

        # Recalculate from ledger
        movimientos = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto
        )
        suma_ledger = sum(m.cantidad_efectiva for m in movimientos)

        self.assertEqual(
            self.producto.stock_actual, suma_ledger,
            f"stock_actual ({self.producto.stock_actual}) != "
            f"Σ movimientos ({suma_ledger}) — Invariant I3 violated",
        )
        self.assertEqual(self.producto.stock_actual, 14)

    def test_I4_stock_anterior_mas_efectivo_igual_resultante(self):
        """
        I4: For every movement, stock_anterior ± cantidad == stock_resultante.

        Verified for each movement in a mixed sequence.
        """
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=20
        )
        MovimientoService.registrar_salida(
            empresa=self.empresa, producto=self.producto, cantidad=7
        )
        MovimientoService.registrar_ajuste(
            empresa=self.empresa, producto=self.producto,
            stock_nuevo=16, motivo="Conteo físico",
        )

        movimientos = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto
        )
        for mov in movimientos:
            expected = mov.stock_anterior + mov.cantidad_efectiva
            self.assertEqual(
                mov.stock_resultante, expected,
                f"I4 violated on movimiento {mov.id} "
                f"(tipo={mov.tipo}): "
                f"{mov.stock_anterior} + ({mov.cantidad_efectiva}) "
                f"= {expected} ≠ {mov.stock_resultante}",
            )

    def test_I5_cantidad_siempre_positiva(self):
        """I5: MovimientoStock.cantidad must be > 0 for every movement type."""
        venta_id = __import__("uuid").uuid4()
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=20
        )
        MovimientoService.registrar_salida(
            empresa=self.empresa, producto=self.producto, cantidad=5
        )
        MovimientoService.registrar_devolucion(
            empresa=self.empresa, producto=self.producto, cantidad=2,
            referencia_tipo="venta", referencia_id=venta_id,
        )
        MovimientoService.registrar_ajuste(
            empresa=self.empresa, producto=self.producto,
            stock_nuevo=14, motivo="Ajuste menor",
        )
        MovimientoService.registrar_merma(
            empresa=self.empresa, producto=self.producto,
            cantidad=1, motivo="Unidad dañada",
        )

        movimientos = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto
        )
        for mov in movimientos:
            self.assertGreater(
                mov.cantidad, 0,
                f"I5 violated: movimiento {mov.id} (tipo={mov.tipo}) "
                f"has cantidad={mov.cantidad}",
            )

    def test_I5_cantidad_cero_rechazada(self):
        """Service must reject cantidad=0 before any DB write."""
        with self.assertRaises(ValidationError):
            MovimientoService.registrar_entrada(
                empresa=self.empresa, producto=self.producto, cantidad=0
            )

    def test_stock_actual_y_ledger_sincronizados_tras_ajuste(self):
        """
        After registrar_ajuste, stock_actual and ledger sum must agree.
        This tests the specific path where delta is negative.
        """
        MovimientoService.registrar_entrada(
            empresa=self.empresa, producto=self.producto, cantidad=30
        )
        MovimientoService.registrar_ajuste(
            empresa=self.empresa, producto=self.producto,
            stock_nuevo=22, motivo="Conteo",
        )
        self.producto.refresh_from_db()

        movimientos = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto
        )
        suma = sum(m.cantidad_efectiva for m in movimientos)
        self.assertEqual(self.producto.stock_actual, suma)
        self.assertEqual(self.producto.stock_actual, 22)


# ─────────────────────────────────────────────────────────────────────────────
# Tenant isolation
# ─────────────────────────────────────────────────────────────────────────────

class TenantAislamientoTest(TestCase):
    """
    Verify that cross-tenant operations are rejected without acquiring any lock.
    """

    def setUp(self):
        ctx_a = setup_producto_con_stock(stock_inicial=10)
        self.empresa_a  = ctx_a["empresa"]
        self.producto_a = ctx_a["producto"]

        ctx_b     = setup_producto_con_stock(stock_inicial=10)
        self.empresa_b = ctx_b["empresa"]

    def test_entrada_producto_otra_empresa_lanza_error(self):
        """empresa B cannot register a movement on empresa A's product."""
        with self.assertRaises(ValidationError) as ctx:
            MovimientoService.registrar_entrada(
                empresa=self.empresa_b, producto=self.producto_a, cantidad=5
            )
        self.assertTrue(hasattr(ctx.exception, "code") or hasattr(ctx.exception, "message_dict") or "no pertenece" in str(ctx.exception).lower())

    def test_salida_producto_otra_empresa_lanza_error(self):
        with self.assertRaises(ValidationError):
            MovimientoService.registrar_salida(
                empresa=self.empresa_b, producto=self.producto_a, cantidad=5
            )

    def test_tenant_mismatch_no_crea_movimiento(self):
        """A tenant mismatch must not create any MovimientoStock record."""
        count_before = MovimientoStock.objects.filter(
            empresa=self.empresa_a, producto=self.producto_a
        ).count()

        with self.assertRaises(ValidationError):
            MovimientoService.registrar_entrada(
                empresa=self.empresa_b, producto=self.producto_a, cantidad=5
            )

        count_after = MovimientoStock.objects.filter(
            empresa=self.empresa_a, producto=self.producto_a
        ).count()
        self.assertEqual(count_before, count_after)


# ─────────────────────────────────────────────────────────────────────────────
import pytest
from django.conf import settings

@pytest.mark.skipif(
    settings.DATABASES["default"]["ENGINE"].endswith("sqlite3"), 
    reason="SQLite no soporta concurrencia real"
)
class ConcurrenciaStockTest(TransactionTestCase):
    """
    Concurrency tests verify that select_for_update() correctly serialises
    concurrent stock mutations on the same product.

    TransactionTestCase is required (not TestCase) because:
        - TestCase wraps every test in a transaction that rolls back at the end.
        - select_for_update() requires REAL transaction boundaries (commits)
          to produce actual row locking between threads.
        - TransactionTestCase flushes the DB between tests using TRUNCATE,
          which is slower but provides the real-commit semantics needed here.

    Thread safety note:
        Results and errors are collected in lists — list.append() is
        thread-safe in CPython (GIL-protected). No explicit lock needed.
    """

    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=10)
        self.empresa  = ctx["empresa"]
        self.admin    = ctx["admin"]
        self.producto = ctx["producto"]

    def test_dos_salidas_concurrentes_mismo_producto(self):
        """
        Two concurrent sale requests for the same product (stock=10).
        Request A: sells 8 units.
        Request B: sells 6 units.

        With select_for_update():
            The winner (A or B) gets the lock, reads stock=10, succeeds.
            The loser re-reads stock after the winner commits:
                If loser is A (8): 10-6=4; 4>=8 is False → StockInsuficienteError
                If loser is B (6): 10-8=2; 2>=6 is False → StockInsuficienteError

        In either case: exactly 1 success, 1 failure, stock >= 0.
        """
        results = []  # True = success, False = StockInsuficienteError
        errors  = []  # unexpected exceptions

        def vender(cantidad):
            try:
                MovimientoService.registrar_salida(
                    empresa=self.empresa,
                    producto=self.producto,
                    cantidad=cantidad,
                    motivo=f"Venta concurrente de {cantidad}",
                )
                results.append(True)
            except StockInsuficienteError:
                results.append(False)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=vender, args=(8,))
        t2 = threading.Thread(target=vender, args=(6,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # No unexpected exceptions
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

        # Exactly one thread succeeded, one failed
        self.assertEqual(results.count(True),  1, f"Expected 1 success, got: {results}")
        self.assertEqual(results.count(False), 1, f"Expected 1 failure, got: {results}")

        # Stock must be >= 0 — Invariant I1
        self.producto.refresh_from_db()
        self.assertGreaterEqual(
            self.producto.stock_actual, 0,
            f"Invariant I1 violated: stock_actual = {self.producto.stock_actual}",
        )

        # Exactly one MovimientoStock created (only the winner's)
        count = MovimientoStock.objects.filter(
            empresa=self.empresa,
            producto=self.producto,
            tipo=TipoMovimiento.SALIDA,
        ).count()
        self.assertEqual(count, 1, "Exactly one SALIDA movement must be persisted")

    def test_salida_concurrente_no_genera_stock_negativo(self):
        """
        With stock=10 and three concurrent requests of 7 each (total demand=21),
        at most one can succeed. Stock must never go negative.
        """
        results = []
        errors  = []

        def vender_siete():
            try:
                MovimientoService.registrar_salida(
                    empresa=self.empresa, producto=self.producto,
                    cantidad=7, motivo="Venta concurrente",
                )
                results.append("ok")
            except StockInsuficienteError:
                results.append("insuficiente")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=vender_siete) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

        # With stock=10 and each request asking 7, at most 1 can succeed
        self.assertLessEqual(
            results.count("ok"), 1,
            f"At most 1 success expected, got: {results}",
        )

        # Stock must never be negative
        self.producto.refresh_from_db()
        self.assertGreaterEqual(
            self.producto.stock_actual, 0,
            f"Invariant I1 violated: stock_actual = {self.producto.stock_actual}",
        )

    def test_entradas_concurrentes_son_todas_exitosas(self):
        """
        Multiple concurrent ENTRADA operations always succeed (no limit).
        All movements must be persisted and stock_actual must equal the sum.
        """
        results = []
        errors  = []
        cantidad_por_thread = 5
        num_threads = 4  # total expected addition: 4 × 5 = 20

        def recibir():
            try:
                MovimientoService.registrar_entrada(
                    empresa=self.empresa, producto=self.producto,
                    cantidad=cantidad_por_thread, motivo="Recepción concurrente",
                )
                results.append(True)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=recibir) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(len(results), num_threads, "All entrada threads must succeed")

        self.producto.refresh_from_db()
        # Initial stock was 10, four entries of 5 = +20
        self.assertEqual(
            self.producto.stock_actual, 10 + (num_threads * cantidad_por_thread),
            f"Final stock should be {10 + num_threads * cantidad_por_thread}, "
            f"got {self.producto.stock_actual}",
        )

        # Verify I3: stock_actual == Σ movimientos
        movimientos = MovimientoStock.objects.filter(
            empresa=self.empresa, producto=self.producto
        )
        suma_ledger = sum(m.cantidad_efectiva for m in movimientos)
        self.assertEqual(self.producto.stock_actual, suma_ledger)
