"""
modules/inventario/services/movimientos.py

MovimientoService — the ONLY authorised point for stock mutations.

════════════════════════════════════════════════════════════════════════════════
Contract
════════════════════════════════════════════════════════════════════════════════

Every public method:
    1. Is decorated with @transaction.atomic.
    2. Acquires a SELECT FOR UPDATE lock on the Producto row before reading
       or modifying stock_actual.
    3. Creates exactly one MovimientoStock entry per call.
    4. Updates Producto.stock_actual in the same atomic transaction as the
       MovimientoStock INSERT — they always agree.
    5. Returns the created MovimientoStock instance.

No code outside this service may update Producto.stock_actual. Views,
serializers, tasks, and other services must call MovimientoService.

════════════════════════════════════════════════════════════════════════════════
Concurrency model
════════════════════════════════════════════════════════════════════════════════

Risk: two concurrent requests reduce stock from the same Producto row.

Without synchronisation (the "lost update" problem):
    T=0ms  A reads stock_actual = 10  (SELECT)
    T=0ms  B reads stock_actual = 10  (SELECT — same value, lock not held)
    T=1ms  A checks 10 >= 8 ✓, computes resultante = 2
    T=1ms  B checks 10 >= 6 ✓, computes resultante = 4
    T=2ms  A writes stock_actual = 2, inserts MovimientoStock(anterior=10, resultante=2)
    T=2ms  B writes stock_actual = 4, inserts MovimientoStock(anterior=10, resultante=4)
    Result: stock = 4 but ledger shows -8 and -6 from 10 → Invariants I3, I4 broken.

With SELECT FOR UPDATE:
    T=0ms  A:  BEGIN; SELECT ... FOR UPDATE → acquires row lock
    T=0ms  B:  BEGIN; SELECT ... FOR UPDATE → BLOCKED (waiting for A's lock)
    T=1ms  A:  checks, computes, inserts movement, updates stock → COMMIT; lock released
    T=1ms  B:  unblocked; re-reads stock_actual = 2 (A's committed value)
    T=1ms  B:  checks 2 >= 6 ✗ → StockInsuficienteError; ROLLBACK

Why F() expressions are NOT sufficient:
    UPDATE producto SET stock = stock - 8  (atomic at UPDATE level)
    ... does not solve the check-then-act problem. The Python-side verification
    (if stock < cantidad: raise) still reads a stale value. Additionally,
    stock_anterior and stock_resultante snapshots in MovimientoStock must
    be calculated from the same locked value — F() does not return that value
    to Python.

Lock granularity: the lock is per-Producto row, not per-table. Two concurrent
requests for DIFFERENT products do not block each other.

════════════════════════════════════════════════════════════════════════════════
Invariants guaranteed by this service
════════════════════════════════════════════════════════════════════════════════

    I1  stock_actual >= 0 if not producto.permite_stock_negativo
    I3  stock_actual == Σ(cantidad_efectiva for all MovimientoStock)
    I4  movimiento.stock_anterior + efectivo(cantidad) == movimiento.stock_resultante
    I5  movimiento.cantidad > 0  (always; tipo determines sign)
    I6  producto.empresa == empresa (tenant isolation)
"""

import logging
from decimal import Decimal
from typing import Optional
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from modules.inventario.exceptions import (
    AjusteInnecesarioError,
    ProductoInactivoError,
    StockInsuficienteError,
)
from modules.inventario.models import (
    MovimientoStock,
    Producto,
    TipoMovimiento,
)
from modules.events.event_bus import EventBus
from modules.events import events

logger = logging.getLogger(__name__)


class MovimientoService:
    """
    Mutation service for Producto stock management.

    All methods are static — no instance state, fully thread-safe.
    All public methods are @transaction.atomic and acquire a SELECT FOR UPDATE
    lock on the Producto row before any read-modify-write cycle.

    Caller contract:
        Pass a Producto *instance* (not an ID). The instance may be stale —
        the service re-fetches it under lock to guarantee freshness.

        Pass referencia_tipo + referencia_id when the movement originates from
        another module (venta, orden_compra, etc.). Both are optional for
        manual/internal operations.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def registrar_entrada(
        empresa,
        producto: Producto,
        cantidad: int,
        motivo: str = "",
        referencia_tipo: str = "",
        referencia_id: Optional[UUID] = None,
        costo_unitario: Optional[Decimal] = None,
        usuario=None,
    ) -> MovimientoStock:
        """
        Record incoming stock: purchase reception, initial stock load, etc.

        An ENTRADA always increases stock_actual unconditionally — there is no
        concept of "insufficient stock" for an incoming movement. The only
        validations are structural: cantidad > 0, producto activo, tenant match.

        Args:
            empresa:         Tenant empresa. Must match producto.empresa.
            producto:        The product receiving stock. Will be re-fetched under lock.
            cantidad:        Units arriving. Must be > 0.
            motivo:          Human-readable description (stored on the movement).
            referencia_tipo: Origin type, e.g. "orden_compra", "stock_inicial".
            referencia_id:   UUID of the originating object.
            costo_unitario:  Unit cost at reception time (for FIFO/valuation).
            usuario:         Usuario triggering the operation (audit trail).

        Returns:
            The created MovimientoStock instance with stock_anterior and
            stock_resultante populated.

        Raises:
            ValidationError:     If cantidad <= 0 or tenant mismatch.
            ProductoInactivoError: If producto.activo is False.
        """
        MovimientoService._validar_cantidad(cantidad)
        MovimientoService._validar_tenant(empresa, producto)

        producto_locked = MovimientoService._lock_producto(empresa, producto.id)
        MovimientoService._validar_activo(producto_locked)

        stock_anterior   = producto_locked.stock_actual
        stock_resultante = stock_anterior + cantidad

        movimiento = MovimientoService._crear_movimiento(
            empresa          = empresa,
            producto         = producto_locked,
            tipo             = TipoMovimiento.ENTRADA,
            cantidad         = cantidad,
            stock_anterior   = stock_anterior,
            stock_resultante = stock_resultante,
            motivo           = motivo,
            referencia_tipo  = referencia_tipo,
            referencia_id    = referencia_id,
            costo_unitario   = costo_unitario,
            usuario          = usuario,
        )

        logger.info(
            "ENTRADA: empresa=%s producto=%s cantidad=%d "
            "(%d → %d) ref=%s/%s",
            empresa.id, producto_locked.id, cantidad,
            stock_anterior, stock_resultante,
            referencia_tipo, referencia_id,
        )
        return movimiento

    @staticmethod
    @transaction.atomic
    def registrar_salida(
        empresa,
        producto: Producto,
        cantidad: int,
        motivo: str = "",
        referencia_tipo: str = "",
        referencia_id: Optional[UUID] = None,
        usuario=None,
    ) -> MovimientoStock:
        """
        Record outgoing stock: sale, internal consumption, etc.

        This is the most concurrency-sensitive operation. SELECT FOR UPDATE
        serialises concurrent calls for the same product — the losing thread
        reads the updated stock_actual after the winner commits, and either
        succeeds with the remaining stock or raises StockInsuficienteError.

        permite_stock_negativo:
            False (default): raises StockInsuficienteError if stock < cantidad.
                             The DB CheckConstraint is a second line of defense.
            True:            proceeds even if stock_resultante < 0. Useful for
                             backorder or presale scenarios.

        Args:
            empresa:         Tenant empresa. Must match producto.empresa.
            producto:        The product losing stock. Will be re-fetched under lock.
            cantidad:        Units leaving. Must be > 0.
            motivo:          Human-readable description.
            referencia_tipo: Origin type, e.g. "venta".
            referencia_id:   UUID of the originating sale.
            usuario:         Usuario triggering the operation (audit trail).

        Returns:
            The created MovimientoStock instance.

        Raises:
            ValidationError:        If cantidad <= 0 or tenant mismatch.
            ProductoInactivoError:  If producto.activo is False.
            StockInsuficienteError: If stock < cantidad and not permite_stock_negativo.
        """
        MovimientoService._validar_cantidad(cantidad)
        MovimientoService._validar_tenant(empresa, producto)

        # ── Acquire lock ────────────────────────────────────────────────────
        # This is the critical section. Thread B is blocked here until Thread A
        # commits. After the lock is acquired, producto_locked.stock_actual
        # reflects the true current stock — no stale reads possible.
        producto_locked = MovimientoService._lock_producto(empresa, producto.id)
        MovimientoService._validar_activo(producto_locked)

        stock_anterior = producto_locked.stock_actual

        # ── Stock sufficiency check ──────────────────────────────────────────
        # Evaluated AFTER acquiring the lock so the check and the subsequent
        # write are atomic at the DB transaction level.
        if not producto_locked.permite_stock_negativo:
            if stock_anterior - cantidad < 0:
                raise StockInsuficienteError(
                    producto  = producto_locked,
                    disponible = stock_anterior,
                    solicitado = cantidad,
                )

        stock_resultante = stock_anterior - cantidad

        movimiento = MovimientoService._crear_movimiento(
            empresa          = empresa,
            producto         = producto_locked,
            tipo             = TipoMovimiento.SALIDA,
            cantidad         = cantidad,
            stock_anterior   = stock_anterior,
            stock_resultante = stock_resultante,
            motivo           = motivo,
            referencia_tipo  = referencia_tipo,
            referencia_id    = referencia_id,
            costo_unitario   = None,
            usuario          = usuario,
        )

        logger.info(
            "SALIDA: empresa=%s producto=%s cantidad=%d "
            "(%d → %d) ref=%s/%s",
            empresa.id, producto_locked.id, cantidad,
            stock_anterior, stock_resultante,
            referencia_tipo, referencia_id,
        )
        return movimiento

    @staticmethod
    @transaction.atomic
    def registrar_ajuste(
        empresa,
        producto: Producto,
        stock_nuevo: int,
        motivo: str,
        usuario=None,
    ) -> MovimientoStock:
        """
        Correct stock_actual to an absolute value (physical count reconciliation).

        ajustar_stock() is the appropriate operation when a physical inventory
        count reveals the recorded stock is wrong. The caller provides the
        REAL counted value; this service computes the delta and selects
        AJUSTE_POSITIVO or AJUSTE_NEGATIVO accordingly.

        Raises AjusteInnecesarioError if stock_nuevo == stock_actual — a count
        that confirms the existing value is not a movement. The caller should
        log this as an audit event rather than calling this method.

        Negative stock_nuevo is accepted only if producto.permite_stock_negativo.
        A negative count after adjustment follows the same rules as registrar_salida.

        Args:
            empresa:     Tenant empresa.
            producto:    Product being adjusted.
            stock_nuevo: The real, physically verified stock count.
            motivo:      Reason for the adjustment (required — auditors need context).
            usuario:     Usuario triggering the operation.

        Returns:
            The created MovimientoStock with tipo=AJUSTE_POSITIVO or AJUSTE_NEGATIVO.

        Raises:
            ValidationError:        If motivo is empty or tenant mismatch.
            ProductoInactivoError:  If producto.activo is False.
            AjusteInnecesarioError: If stock_nuevo == stock_actual (no delta).
            StockInsuficienteError: If stock_nuevo < 0 and not permite_stock_negativo.
        """
        if not motivo or not motivo.strip():
            raise ValidationError(
                "El motivo es obligatorio para registrar un ajuste de stock.",
                code="motivo_requerido",
            )
        MovimientoService._validar_tenant(empresa, producto)

        producto_locked = MovimientoService._lock_producto(empresa, producto.id)
        MovimientoService._validar_activo(producto_locked)

        stock_anterior = producto_locked.stock_actual
        delta          = stock_nuevo - stock_anterior

        if delta == 0:
            raise AjusteInnecesarioError(
                producto     = producto_locked,
                stock_actual = stock_anterior,
            )

        # Negative result: validate against permite_stock_negativo
        if stock_nuevo < 0 and not producto_locked.permite_stock_negativo:
            raise StockInsuficienteError(
                producto   = producto_locked,
                disponible = stock_anterior,
                solicitado = stock_anterior - stock_nuevo,  # magnitude of the reduction
            )

        tipo     = TipoMovimiento.AJUSTE_POSITIVO if delta > 0 else TipoMovimiento.AJUSTE_NEGATIVO
        cantidad = abs(delta)

        movimiento = MovimientoService._crear_movimiento(
            empresa          = empresa,
            producto         = producto_locked,
            tipo             = tipo,
            cantidad         = cantidad,
            stock_anterior   = stock_anterior,
            stock_resultante = stock_nuevo,
            motivo           = motivo.strip(),
            referencia_tipo  = "ajuste_manual",
            referencia_id    = None,
            costo_unitario   = None,
            usuario          = usuario,
        )

        logger.info(
            "AJUSTE: empresa=%s producto=%s tipo=%s delta=%+d "
            "(%d → %d) motivo='%s'",
            empresa.id, producto_locked.id, tipo, delta,
            stock_anterior, stock_nuevo, motivo,
        )
        return movimiento

    @staticmethod
    @transaction.atomic
    def registrar_devolucion(
        empresa,
        producto: Producto,
        cantidad: int,
        referencia_tipo: str,
        referencia_id: UUID,
        motivo: str = "",
        costo_unitario: Optional[Decimal] = None,
        usuario=None,
    ) -> MovimientoStock:
        """
        Record a customer return that restores stock.

        A DEVOLUCION is structurally identical to an ENTRADA (it increases
        stock) but carries a different tipo for reporting purposes:
            - ENTRADA: fresh stock from a supplier
            - DEVOLUCION: returned stock from a prior sale

        referencia_tipo + referencia_id are REQUIRED (unlike registrar_entrada
        where they are optional). A return without a traceable origin is an
        AJUSTE_POSITIVO, not a DEVOLUCION.

        Args:
            empresa:         Tenant empresa.
            producto:        Product being returned.
            cantidad:        Units being returned. Must be > 0.
            referencia_tipo: Type of origin, typically "venta".
            referencia_id:   UUID of the original sale.
            motivo:          Optional description.
            costo_unitario:  Unit cost for valuation (usually from the original sale).
            usuario:         Usuario triggering the operation.

        Returns:
            The created MovimientoStock with tipo=DEVOLUCION.

        Raises:
            ValidationError:       If referencia_tipo/referencia_id are missing,
                                   cantidad <= 0, or tenant mismatch.
            ProductoInactivoError: If producto.activo is False.
        """
        MovimientoService._validar_cantidad(cantidad)
        MovimientoService._validar_tenant(empresa, producto)

        if not referencia_tipo or referencia_id is None:
            raise ValidationError(
                "Una devolución requiere referencia_tipo y referencia_id. "
                "Para ajustes sin origen conocido, use registrar_ajuste().",
                code="referencia_requerida",
            )

        producto_locked  = MovimientoService._lock_producto(empresa, producto.id)
        MovimientoService._validar_activo(producto_locked)

        stock_anterior   = producto_locked.stock_actual
        stock_resultante = stock_anterior + cantidad

        movimiento = MovimientoService._crear_movimiento(
            empresa          = empresa,
            producto         = producto_locked,
            tipo             = TipoMovimiento.DEVOLUCION,
            cantidad         = cantidad,
            stock_anterior   = stock_anterior,
            stock_resultante = stock_resultante,
            motivo           = motivo,
            referencia_tipo  = referencia_tipo,
            referencia_id    = referencia_id,
            costo_unitario   = costo_unitario,
            usuario          = usuario,
        )

        logger.info(
            "DEVOLUCION: empresa=%s producto=%s cantidad=%d "
            "(%d → %d) ref=%s/%s",
            empresa.id, producto_locked.id, cantidad,
            stock_anterior, stock_resultante,
            referencia_tipo, referencia_id,
        )
        return movimiento

    @staticmethod
    @transaction.atomic
    def registrar_merma(
        empresa,
        producto: Producto,
        cantidad: int,
        motivo: str,
        usuario=None,
    ) -> MovimientoStock:
        """
        Record stock loss due to damage, expiry, theft, or spillage.

        MERMA is reported separately from SALIDA so the Reportes module can
        calculate:
            gross margin  = revenue - (SALIDA × costo_unitario)
            shrinkage     = MERMA × costo_unitario

        Blending MERMA with SALIDA would inflate COGS and hide operational
        losses in the financial reports.

        Respects permite_stock_negativo the same way as registrar_salida:
        if the product does not allow negative stock, merma cannot exceed
        current stock_actual.

        motivo is required — auditors need context for every loss record.

        Args:
            empresa:   Tenant empresa.
            producto:  Product with the loss.
            cantidad:  Units lost. Must be > 0.
            motivo:    Reason for the loss (required).
            usuario:   Usuario triggering the operation.

        Returns:
            The created MovimientoStock with tipo=MERMA.

        Raises:
            ValidationError:        If motivo is empty, cantidad <= 0, or tenant mismatch.
            ProductoInactivoError:  If producto.activo is False.
            StockInsuficienteError: If stock < cantidad and not permite_stock_negativo.
        """
        MovimientoService._validar_cantidad(cantidad)
        MovimientoService._validar_tenant(empresa, producto)

        if not motivo or not motivo.strip():
            raise ValidationError(
                "El motivo es obligatorio para registrar una merma.",
                code="motivo_requerido",
            )

        producto_locked = MovimientoService._lock_producto(empresa, producto.id)
        MovimientoService._validar_activo(producto_locked)

        stock_anterior = producto_locked.stock_actual

        if not producto_locked.permite_stock_negativo:
            if stock_anterior - cantidad < 0:
                raise StockInsuficienteError(
                    producto   = producto_locked,
                    disponible = stock_anterior,
                    solicitado = cantidad,
                )

        stock_resultante = stock_anterior - cantidad

        movimiento = MovimientoService._crear_movimiento(
            empresa          = empresa,
            producto         = producto_locked,
            tipo             = TipoMovimiento.MERMA,
            cantidad         = cantidad,
            stock_anterior   = stock_anterior,
            stock_resultante = stock_resultante,
            motivo           = motivo.strip(),
            referencia_tipo  = "",
            referencia_id    = None,
            costo_unitario   = None,
            usuario          = usuario,
        )

        logger.info(
            "MERMA: empresa=%s producto=%s cantidad=%d "
            "(%d → %d) motivo='%s'",
            empresa.id, producto_locked.id, cantidad,
            stock_anterior, stock_resultante, motivo,
        )
        return movimiento

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _validar_cantidad(cantidad: int) -> None:
        """
        Enforce Invariant I5: cantidad must be strictly positive.

        Called before _lock_producto so bad inputs are rejected without
        hitting the DB at all.

        Args:
            cantidad: The proposed movement quantity.

        Raises:
            ValidationError: If cantidad <= 0.
        """
        if not isinstance(cantidad, int) or cantidad <= 0:
            raise ValidationError(
                f"La cantidad debe ser un entero positivo. Recibido: {cantidad!r}",
                code="cantidad_invalida",
            )

    @staticmethod
    def _validar_tenant(empresa, producto: Producto) -> None:
        """
        Enforce Invariant I6: producto must belong to empresa.

        Compares empresa_id values in memory — no extra DB query.
        This guard runs BEFORE _lock_producto so a cross-tenant call
        is rejected without acquiring any lock.

        Args:
            empresa: The request's tenant.
            producto: The product being operated on.

        Raises:
            ValidationError: If producto.empresa_id != empresa.id.
        """
        if str(producto.empresa_id) != str(empresa.id):
            raise ValidationError(
                "El producto no pertenece a esta empresa.",
                code="tenant_mismatch",
            )

    @staticmethod
    def _validar_activo(producto: Producto) -> None:
        """
        Reject operations on inactive products.

        Called AFTER _lock_producto so the check uses the freshly locked
        value — not a potentially stale instance passed by the caller.

        Args:
            producto: The freshly locked Producto instance.

        Raises:
            ProductoInactivoError: If producto.activo is False.
        """
        if not producto.activo:
            raise ProductoInactivoError(producto)

    @staticmethod
    def _lock_producto(empresa, producto_id) -> Producto:
        """
        Acquire a SELECT FOR UPDATE row lock on the Producto and return
        a fresh, locked instance.

        This is the concurrency boundary. All callers must use the returned
        instance — not the stale one passed to the public method — for
        stock_actual reads and writes.

        Must be called inside an active @transaction.atomic block.
        Django raises an exception if select_for_update() is used outside a
        transaction; the @transaction.atomic on the caller guarantees this.

        Args:
            empresa:     Tenant scope for the query.
            producto_id: UUID of the product to lock.

        Returns:
            A freshly fetched, row-locked Producto instance.

        Raises:
            Producto.DoesNotExist: If the product does not exist or belongs
                                   to a different empresa (tenant safety).
        """
        return (
            Producto.objects
            .select_for_update()
            .get(id=producto_id, empresa=empresa)
        )

    @staticmethod
    def _crear_movimiento(
        empresa,
        producto: Producto,
        tipo: str,
        cantidad: int,
        stock_anterior: int,
        stock_resultante: int,
        motivo: str,
        referencia_tipo: str,
        referencia_id: Optional[UUID],
        costo_unitario: Optional[Decimal],
        usuario,
    ) -> MovimientoStock:
        """
        INSERT a MovimientoStock and UPDATE Producto.stock_actual atomically.

        Both writes happen in the caller's transaction. If either fails
        (e.g. the DB CheckConstraint on stock_actual fires), the entire
        transaction rolls back and no partial state is persisted.

        Invariants verified by DB constraints:
            I4: check_movimiento_stock_resultante_coherente
                (stock_resultante within [stock_anterior - cantidad,
                                         stock_anterior + cantidad])
            I5: check_movimiento_cantidad_positiva (cantidad > 0)
            I1: check_producto_stock_no_negativo
                (stock_actual >= 0 if not permite_stock_negativo)

        Args:
            empresa:          Tenant.
            producto:         Locked Producto instance.
            tipo:             TipoMovimiento value.
            cantidad:         Units moved (positive).
            stock_anterior:   Snapshot of stock_actual before this movement.
            stock_resultante: Computed stock_actual after this movement.
            motivo:           Human-readable reason.
            referencia_tipo:  Origin type string.
            referencia_id:    Origin UUID.
            costo_unitario:   Optional unit cost for valuation.
            usuario:          Audit user.

        Returns:
            The created, saved MovimientoStock instance.
        """
        movimiento = MovimientoStock.objects.create(
            empresa          = empresa,
            producto         = producto,
            tipo             = tipo,
            cantidad         = cantidad,
            stock_anterior   = stock_anterior,
            stock_resultante = stock_resultante,
            motivo           = motivo,
            referencia_tipo  = referencia_tipo or "",
            referencia_id    = referencia_id,
            costo_unitario   = costo_unitario,
            created_by       = usuario,
            updated_by       = usuario,
        )

        # Update the denormalized cache in the same atomic block.
        # update_fields is explicit to avoid overwriting concurrent changes
        # to unrelated fields (e.g. nombre, precio_venta).
        producto.stock_actual = stock_resultante
        producto.updated_by   = usuario
        producto.save(update_fields=["stock_actual", "updated_by", "updated_at"])

        EventBus.publish(
            events.STOCK_ACTUALIZADO,
            empresa_id=empresa.id,
            usuario_id=usuario.id if usuario else None,
            recurso="producto",
            recurso_id=producto.id,
            stock_anterior=float(stock_anterior),
            stock_resultante=float(stock_resultante),
            cantidad=float(cantidad),
            tipo=tipo
        )

        return movimiento
