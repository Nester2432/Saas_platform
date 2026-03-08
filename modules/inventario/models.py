"""
modules/inventario/models.py

Stock management module models.

────────────────────────────────────────────────────────────────────────────────
Core concept: the stock ledger
────────────────────────────────────────────────────────────────────────────────

Stock is NOT a mutable counter. It is an append-only ledger of movements, and
`Producto.stock_actual` is a *denormalized cache* of the ledger sum:

    stock_actual = Σ(+cantidad for ENTRADA, DEVOLUCION, AJUSTE_POSITIVO)
                 - Σ(+cantidad for SALIDA, MERMA, AJUSTE_NEGATIVO)

`stock_actual` exists for query performance. Its only source of truth is the
MovimientoStock table. MovimientoService keeps both in sync atomically.

────────────────────────────────────────────────────────────────────────────────
Model map
────────────────────────────────────────────────────────────────────────────────

    CategoriaProducto    → flat taxonomy for UI grouping ("Bebidas", "Electrónica")
    Producto             → the inventoriable item; owns stock_actual cache
    MovimientoStock      → immutable ledger entry; every stock change lives here
    Proveedor            → vendor from whom stock is purchased
    OrdenCompra          → a purchase order sent to a Proveedor
    OrdenCompraDetalle   → line item inside an OrdenCompra (one per product)

────────────────────────────────────────────────────────────────────────────────
Invariants (enforced in MovimientoService — DB constraints where expressible)
────────────────────────────────────────────────────────────────────────────────

    I1  stock_actual >= 0  IF NOT producto.permite_stock_negativo
    I2  MovimientoStock is append-only and never soft-deleted
    I3  stock_actual  == Σ of all MovimientoStock for the product
    I4  movimiento.stock_anterior + effective(cantidad) == movimiento.stock_resultante
    I5  movimiento.cantidad > 0  (always positive; tipo determines accounting sign)
    I6  Every entity belongs to exactly one empresa (tenant isolation)

────────────────────────────────────────────────────────────────────────────────
Concurrency
────────────────────────────────────────────────────────────────────────────────

Concurrent stock mutations are serialised with select_for_update() on Producto:

    with transaction.atomic():
        producto = Producto.objects.select_for_update().get(id=..., empresa=empresa)
        # Thread B is blocked here until Thread A commits.
        if not producto.permite_stock_negativo:
            assert producto.stock_actual >= cantidad
        producto.stock_actual = new_value
        producto.save(update_fields=["stock_actual", ...])
        MovimientoStock.objects.create(...)

The lock is on the Producto row (not on MovimientoStock rows) because the
invariant depends on a single value: stock_actual. MovimientoStock rows are
always inserts — they never compete with each other.

DB-level defense: CheckConstraint on stock_actual >= 0 provides a last-resort
guarantee even if service code is bypassed (e.g. direct ORM calls in scripts).
For products where permite_stock_negativo=True the constraint does not apply —
this is intentional and matches the business rule.

Note: A PostgreSQL ExclusionConstraint with btree_gist could enforce non-overlap
at the DB level for time-range inventory reservations, but that is not in scope
for this module. select_for_update() is sufficient for concurrent point-in-time
stock mutations.

────────────────────────────────────────────────────────────────────────────────
Index strategy
────────────────────────────────────────────────────────────────────────────────

Every index starts with `empresa`. In a multi-tenant shared database, every
query is always tenant-scoped first. Single-column indexes on these tables
are almost never useful — composite (empresa, …) indexes cover all cases.

The MovimientoStock index on (empresa, producto, -created_at) covers:
    - "History for this product" (audit trail, sorted newest first)
    - Pagination of movement history (most common read pattern)
    - Reconciliation queries (sum of last N movements)
"""

from django.core.exceptions import ValidationError
from django.db import models

from core.models import EmpresaModel


# ─────────────────────────────────────────────────────────────────────────────
# Choice enumerations
# ─────────────────────────────────────────────────────────────────────────────

class TipoMovimiento(models.TextChoices):
    """
    Accounting type for a MovimientoStock entry.

    Sign convention (enforced in MovimientoService):
        ADDS  to stock_actual: ENTRADA, DEVOLUCION, AJUSTE_POSITIVO
        REMOVES from stock_actual: SALIDA, MERMA, AJUSTE_NEGATIVO

    cantidad in MovimientoStock is ALWAYS POSITIVE (see Invariant I5).
    The sign is determined by this type, never by the sign of cantidad.

    Rationale for separating AJUSTE_POSITIVO / AJUSTE_NEGATIVO:
        A single AJUSTE type with a signed quantity would require allowing
        negative values in the cantidad field, which breaks Invariant I5 and
        makes aggregate queries ("total units received this month") ambiguous.
        Two explicit types keep the field always positive and queries unambiguous.

    Rationale for separating MERMA from SALIDA:
        Both reduce stock, but MERMA (shrinkage: damage, expiry, theft) is
        distinct from a commercial SALIDA. Reportes module needs this split
        to calculate gross margin vs. shrinkage loss separately.

    Rationale for separating DEVOLUCION from ENTRADA:
        A DEVOLUCION restores stock from a prior SALIDA and carries a
        referencia_id pointing to the original sale. Blending it with ENTRADA
        would lose that traceability and distort "units purchased" reports.
    """

    ENTRADA          = "ENTRADA",          "Entrada de stock"
    SALIDA           = "SALIDA",           "Salida por venta"
    AJUSTE_POSITIVO  = "AJUSTE_POSITIVO",  "Ajuste positivo (conteo físico)"
    AJUSTE_NEGATIVO  = "AJUSTE_NEGATIVO",  "Ajuste negativo (conteo físico)"
    DEVOLUCION       = "DEVOLUCION",       "Devolución de cliente"
    MERMA            = "MERMA",            "Merma (daño / vencimiento / robo)"

    @classmethod
    def tipos_positivos(cls):
        """Types that ADD to stock_actual."""
        return {cls.ENTRADA, cls.DEVOLUCION, cls.AJUSTE_POSITIVO}

    @classmethod
    def tipos_negativos(cls):
        """Types that REMOVE from stock_actual."""
        return {cls.SALIDA, cls.MERMA, cls.AJUSTE_NEGATIVO}


class EstadoOrdenCompra(models.TextChoices):
    """
    Lifecycle states for a purchase order.

    Valid transitions (enforced in OrdenCompraService):

        BORRADOR ──enviar──► ENVIADA ──recibir parcial──► RECIBIDA_PARCIAL
                                  │                              │
                                  │                    ──recibir completo──►RECIBIDA_COMPLETA
                                  │
                                  └──cancelar──► CANCELADA

        BORRADOR ──cancelar──► CANCELADA

    Terminal states (no further transitions):
        RECIBIDA_COMPLETA, CANCELADA

    Note: RECIBIDA_PARCIAL is not terminal — further recepciones are allowed
    until the order is fully received. Each recepcion call invokes
    MovimientoService.registrar_entrada() for the newly received quantities.
    """
    BORRADOR           = "BORRADOR",           "Borrador"
    ENVIADA            = "ENVIADA",            "Enviada al proveedor"
    RECIBIDA_PARCIAL   = "RECIBIDA_PARCIAL",   "Recibida parcialmente"
    RECIBIDA_COMPLETA  = "RECIBIDA_COMPLETA",  "Recibida completamente"
    CANCELADA          = "CANCELADA",          "Cancelada"


# ─────────────────────────────────────────────────────────────────────────────
# CategoriaProducto
# ─────────────────────────────────────────────────────────────────────────────

class CategoriaProducto(EmpresaModel):
    """
    Flat product taxonomy for UI grouping and filtering.

    Deliberately flat (no parent FK) to avoid recursive query complexity.
    If a tree hierarchy becomes necessary, migrate to a closure table or
    use django-treebeard — but do not anticipate that complexity here.

    `orden` provides explicit display ordering without relying on name sort,
    which is fragile when names are translated or rebranded.
    """

    nombre      = models.CharField(max_length=100)
    descripcion = models.TextField(blank=True)
    color       = models.CharField(
        max_length=7,
        default="#6B7280",
        help_text="Hex color code for UI display, e.g. '#3B82F6'.",
    )
    orden       = models.PositiveSmallIntegerField(
        default=0,
        help_text="Display order within the empresa's category list.",
    )

    class Meta:
        db_table         = "inventario_categoria_producto"
        verbose_name     = "Categoría de Producto"
        verbose_name_plural = "Categorías de Productos"
        ordering         = ["orden", "nombre"]
        constraints      = [
            models.UniqueConstraint(
                fields=["empresa", "nombre"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_categoria_nombre_por_empresa",
            ),
        ]
        indexes = [
            # "List all categories for this empresa" (category picker in UI)
            models.Index(
                fields=["empresa", "orden", "nombre"],
                name="idx_categoria_empresa_orden",
            ),
        ]

    def __str__(self):
        return self.nombre


# ─────────────────────────────────────────────────────────────────────────────
# Producto
# ─────────────────────────────────────────────────────────────────────────────

class Producto(EmpresaModel):
    """
    An inventoriable item belonging to an empresa.

    stock_actual is a DENORMALIZED CACHE of the MovimientoStock ledger sum.
    It MUST NOT be updated directly outside of MovimientoService. Doing so
    would break Invariant I3 (stock_actual == Σ movimientos) and corrupt the
    audit trail permanently.

    The only correct way to change stock is:
        MovimientoService.registrar_entrada(...)
        MovimientoService.registrar_salida(...)
        MovimientoService.ajustar_stock(...)
        MovimientoService.registrar_merma(...)
        MovimientoService.registrar_devolucion(...)

    permite_stock_negativo is a per-product business decision:
        False (default): service raises StockInsuficienteError on oversell.
                         DB CheckConstraint enforces stock_actual >= 0.
        True:            allows backorder/presale scenarios.
                         The DB constraint does not apply to these products.
    The constraint is defined with a partial WHERE clause:
        CHECK (permite_stock_negativo OR stock_actual >= 0)

    codigo is the SKU / barcode / internal reference. It must be unique within
    an empresa but not globally — different empresas may use the same SKU.

    precio_costo / precio_venta live here for:
        - Inventory valuation (Σ stock_actual × precio_costo)
        - Default suggestion in VentaService (can be overridden per sale)
    They are snapshots — historical sales retain their own precio in DetalleVenta.

    unidad_medida is free text ("unidades", "kg", "litros") rather than a
    fixed choice because SMBs use very diverse units and the UI just needs to
    display the label.
    """

    nombre      = models.CharField(
        max_length=200,
        help_text="Full product name as shown in UI and reports.",
    )
    codigo      = models.CharField(
        max_length=100,
        blank=True,
        help_text="SKU, barcode, or internal reference code. Unique per empresa.",
    )
    descripcion = models.TextField(blank=True)
    categoria   = models.ForeignKey(
        CategoriaProducto,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="productos",
        help_text="Optional category for filtering and reporting.",
    )
    precio_costo = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Purchase cost per unit. Used for inventory valuation.",
    )
    precio_venta = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Suggested sale price per unit. "
            "VentaService may use this as a default — actual price is on DetalleVenta."
        ),
    )
    stock_actual = models.IntegerField(
        default=0,
        help_text=(
            "Denormalized cache of Σ MovimientoStock. "
            "NEVER update directly. Always use MovimientoService."
        ),
    )
    stock_minimo = models.IntegerField(
        default=0,
        help_text=(
            "Reorder threshold. When stock_actual <= stock_minimo, "
            "the product appears in the low-stock alert list."
        ),
    )
    stock_maximo = models.IntegerField(
        null=True,
        blank=True,
        help_text=(
            "Optional upper bound. When stock_actual >= stock_maximo, "
            "the product appears in the overstock alert list."
        ),
    )
    unidad_medida = models.CharField(
        max_length=50,
        default="unidades",
        help_text="Display label for the stock unit, e.g. 'kg', 'litros', 'unidades'.",
    )
    permite_stock_negativo = models.BooleanField(
        default=False,
        help_text=(
            "If True, stock_actual may go below zero (backorder / presale). "
            "If False, MovimientoService raises StockInsuficienteError on oversell."
        ),
    )
    activo = models.BooleanField(
        default=True,
        help_text="Inactive products are hidden from sale and booking flows.",
    )

    class Meta:
        db_table             = "inventario_producto"
        verbose_name         = "Producto"
        verbose_name_plural  = "Productos"
        ordering             = ["nombre"]
        constraints = [
            # Stock cannot go negative for products that don't allow it.
            # Expressed as: permite_stock_negativo OR stock_actual >= 0.
            # This is the DB-level last-resort guard — MovimientoService
            # raises StockInsuficienteError before ever reaching this.
            models.CheckConstraint(
                check=(
                    models.Q(permite_stock_negativo=True)
                    | models.Q(stock_actual__gte=0)
                ),
                name="check_producto_stock_no_negativo",
            ),
            # stock_minimo must be non-negative.
            models.CheckConstraint(
                check=models.Q(stock_minimo__gte=0),
                name="check_producto_stock_minimo_no_negativo",
            ),
            # stock_maximo, when set, must be >= stock_minimo.
            # Expressed as: stock_maximo IS NULL OR stock_maximo >= stock_minimo.
            models.CheckConstraint(
                check=(
                    models.Q(stock_maximo__isnull=True)
                    | models.Q(stock_maximo__gte=models.F("stock_minimo"))
                ),
                name="check_producto_stock_maximo_gte_minimo",
            ),
            # codigo must be unique within an empresa (when not empty).
            # Partial: only enforced when codigo is non-empty and not soft-deleted.
            models.UniqueConstraint(
                fields=["empresa", "codigo"],
                condition=models.Q(
                    deleted_at__isnull=True,
                    codigo__gt="",      # excludes empty-string codes
                ),
                name="unique_producto_codigo_por_empresa",
            ),
        ]
        indexes = [
            # ── Primary catalog query ─────────────────────────────────────
            # "List all active products for this empresa"
            # Covers the main product list endpoint with activo filter.
            models.Index(
                fields=["empresa", "activo", "nombre"],
                name="idx_producto_empresa_activo",
            ),
            # ── Low-stock alerts ──────────────────────────────────────────
            # "Which products are at or below reorder threshold?"
            # ProductoService.get_productos_bajo_stock():
            #   WHERE empresa=? AND activo=True AND stock_actual <= stock_minimo
            # Partial index possible here in PostgreSQL but Django's Index
            # does not support partial indexes — service adds activo filter.
            models.Index(
                fields=["empresa", "stock_actual"],
                name="idx_producto_empresa_stock",
            ),
            # ── Category filtering ────────────────────────────────────────
            # "Products in this category"
            models.Index(
                fields=["empresa", "categoria", "activo"],
                name="idx_producto_empresa_categoria",
            ),
            # ── SKU lookup ────────────────────────────────────────────────
            # "Find product by barcode / SKU" (POS / mobile scanner flow)
            models.Index(
                fields=["empresa", "codigo"],
                name="idx_producto_empresa_codigo",
            ),
        ]

    @property
    def esta_bajo_stock(self) -> bool:
        """True when stock_actual is at or below the reorder threshold."""
        return self.stock_actual <= self.stock_minimo

    @property
    def esta_sobre_stock(self) -> bool:
        """True when stock_maximo is set and stock_actual has reached or exceeded it."""
        return self.stock_maximo is not None and self.stock_actual >= self.stock_maximo

    def __str__(self):
        codigo_str = f" [{self.codigo}]" if self.codigo else ""
        return f"{self.nombre}{codigo_str}"


# ─────────────────────────────────────────────────────────────────────────────
# MovimientoStock
# ─────────────────────────────────────────────────────────────────────────────

class MovimientoStock(EmpresaModel):
    """
    Immutable ledger entry for every stock change.

    ────────────────────────────────────────────────────────────────────────
    Immutability contract
    ────────────────────────────────────────────────────────────────────────
    MovimientoStock is NEVER updated or deleted after creation — not even
    soft-deleted. deleted_at is inherited from EmpresaModel but must remain
    NULL forever. Corrections are made by creating a new compensating entry
    (e.g. an AJUSTE_POSITIVO to undo an incorrect SALIDA).

    This constraint cannot be expressed as a DB CheckConstraint (it requires
    trigger logic), so it is enforced by convention + code review + the
    fact that no MovimientoService method ever calls .save() on an existing
    MovimientoStock instance.

    ────────────────────────────────────────────────────────────────────────
    Fields
    ────────────────────────────────────────────────────────────────────────
    cantidad:
        Always strictly positive (Invariant I5). PositiveIntegerField enforces
        this at the DB level (CHECK (cantidad > 0)).
        The accounting sign is determined by TipoMovimiento, not by this value.

    stock_anterior / stock_resultante:
        Snapshots of Producto.stock_actual immediately before and after this
        movement. Stored atomically with the movement creation.

        Invariant I4: stock_anterior + effective(cantidad) == stock_resultante
        where effective(cantidad) = +cantidad if tipo in tipos_positivos()
                                  = -cantidad if tipo in tipos_negativos()

        These snapshots allow point-in-time stock reconstruction without
        replaying the entire ledger, and make individual movement auditing
        trivially readable: "stock went from 45 to 32 because 13 units were sold."

    referencia_tipo / referencia_id:
        Cross-module reference without Django's GenericForeignKey.
        referencia_tipo is a free string like "venta", "orden_compra", "ajuste_manual".
        referencia_id is the UUID of the referenced object.

        Rationale for NOT using GenericForeignKey:
            - GenericFK requires ContentType table lookups (extra query)
            - Loses type safety — any model UUID can be set
            - Makes bulk queries like "all movements from sales" require a JOIN
              through ContentType instead of a simple string filter
            - Module boundaries: inventario should not know the ORM models of
              ventas/turnos. The string reference preserves that boundary.

        Referencing: VentaService calls MovimientoService.registrar_salida(
            referencia_tipo="venta", referencia_id=venta.id, ...
        ) — the inventario module never imports from modules.ventas.

    costo_unitario:
        Unit cost at the time of the movement. Used for inventory valuation
        (FIFO, LIFO, weighted average) in the Reportes module. NULL means
        the cost was not recorded (e.g. manual adjustments, free stock).
    """

    producto = models.ForeignKey(
        Producto,
        on_delete=models.PROTECT,
        related_name="movimientos",
        help_text="The product whose stock changed.",
    )
    tipo = models.CharField(
        max_length=30,
        choices=TipoMovimiento.choices,
        help_text="Accounting type — determines whether cantidad adds or subtracts.",
    )
    cantidad = models.PositiveIntegerField(
        help_text=(
            "Units moved. Always strictly positive (Invariant I5). "
            "Sign is determined by TipoMovimiento, not by this field."
        ),
    )
    stock_anterior = models.IntegerField(
        help_text="Snapshot of Producto.stock_actual BEFORE this movement.",
    )
    stock_resultante = models.IntegerField(
        help_text=(
            "Snapshot of Producto.stock_actual AFTER this movement. "
            "Must equal stock_anterior ± cantidad (Invariant I4)."
        ),
    )
    referencia_tipo = models.CharField(
        max_length=50,
        blank=True,
        help_text=(
            "Type of originating object, e.g. 'venta', 'orden_compra', 'ajuste_manual'. "
            "Pair with referencia_id for cross-module traceability."
        ),
    )
    referencia_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="UUID of the originating object (venta.id, orden_compra.id, etc.).",
    )
    motivo = models.TextField(
        blank=True,
        help_text="Human-readable reason for the movement.",
    )
    costo_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Cost per unit at the time of movement. "
            "Used for inventory valuation in Reportes. NULL if not applicable."
        ),
    )

    class Meta:
        db_table            = "inventario_movimiento_stock"
        verbose_name        = "Movimiento de Stock"
        verbose_name_plural = "Movimientos de Stock"
        # Default: newest movements first — matches the most common audit query.
        ordering            = ["-created_at"]
        constraints = [
            # cantidad must be strictly positive (Invariant I5).
            # PositiveIntegerField generates CHECK (cantidad >= 0) at the DB level,
            # but "= 0" is semantically meaningless for a movement. This constraint
            # makes the "strictly positive" requirement explicit and machine-checkable.
            models.CheckConstraint(
                check=models.Q(cantidad__gt=0),
                name="check_movimiento_cantidad_positiva",
            ),
            # Invariant I4: stock_resultante must equal stock_anterior +/- cantidad.
            # Expressed as a range: stock_resultante is within
            # [stock_anterior - cantidad, stock_anterior + cantidad].
            # This catches the most common bug (wrong sign) at the DB level.
            models.CheckConstraint(
                check=(
                    models.Q(
                        stock_resultante__gte=models.F("stock_anterior") - models.F("cantidad")
                    )
                    & models.Q(
                        stock_resultante__lte=models.F("stock_anterior") + models.F("cantidad")
                    )
                ),
                name="check_movimiento_stock_resultante_coherente",
            ),
            # referencia_id requires referencia_tipo to be non-empty.
            # It is semantically incorrect to have an ID with no type context.
            models.CheckConstraint(
                check=(
                    models.Q(referencia_id__isnull=True)
                    | models.Q(referencia_tipo__gt="")
                ),
                name="check_movimiento_referencia_tipo_requerido",
            ),
        ]
        indexes = [
            # ── Primary audit query ───────────────────────────────────────
            # "Movement history for this product, newest first"
            # This is the single most important index in the inventario module:
            # covers the movement audit trail (paginated, sorted by -created_at)
            # and the stock reconciliation query (Σ of all movements for a product).
            # The DESC on created_at is expressed as a negative prefix in the field list.
            models.Index(
                fields=["empresa", "producto", "-created_at"],
                name="idx_movimiento_empresa_producto_fecha",
            ),
            # ── Type filtering ────────────────────────────────────────────
            # "All SALIDA movements for this empresa" (shrinkage report)
            # "All ENTRADA movements this month" (purchase volume report)
            models.Index(
                fields=["empresa", "tipo", "-created_at"],
                name="idx_movimiento_empresa_tipo",
            ),
            # ── Cross-module traceability ─────────────────────────────────
            # "All movements that originated from a specific venta"
            # Used by Reportes: join inventario movements to a sale record.
            models.Index(
                fields=["empresa", "referencia_tipo", "referencia_id"],
                name="idx_movimiento_empresa_referencia",
            ),
        ]

    @property
    def es_positivo(self) -> bool:
        """True if this movement adds to stock_actual."""
        return self.tipo in TipoMovimiento.tipos_positivos()

    @property
    def cantidad_efectiva(self) -> int:
        """
        Signed quantity: positive if the movement adds to stock, negative if it removes.
        Used in aggregate calculations and in the service-layer invariant check.
        """
        return self.cantidad if self.es_positivo else -self.cantidad

    def __str__(self):
        signo = "+" if self.es_positivo else "-"
        return (
            f"{self.get_tipo_display()}: {signo}{self.cantidad} "
            f"{self.producto} "
            f"({self.stock_anterior} → {self.stock_resultante})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Proveedor
# ─────────────────────────────────────────────────────────────────────────────

class Proveedor(EmpresaModel):
    """
    A vendor from whom the empresa purchases stock.

    Linked to OrdenCompra; not directly linked to MovimientoStock.
    The chain is: Proveedor → OrdenCompra → recibir_orden() → MovimientoStock(ENTRADA).

    cuit is the Argentine tax ID (CUIT/CUIL). Stored as a plain string to
    accommodate different formats (with/without hyphens) and future
    internationalization without a schema migration.
    """

    nombre    = models.CharField(max_length=200)
    cuit      = models.CharField(
        max_length=20,
        blank=True,
        help_text="CUIT/CUIL or equivalent tax identifier.",
    )
    email     = models.EmailField(blank=True)
    telefono  = models.CharField(max_length=30, blank=True)
    direccion = models.TextField(blank=True)
    activo    = models.BooleanField(
        default=True,
        help_text="Inactive vendors do not appear in the new-order flow.",
    )
    notas     = models.TextField(blank=True)

    class Meta:
        db_table            = "inventario_proveedor"
        verbose_name        = "Proveedor"
        verbose_name_plural = "Proveedores"
        ordering            = ["nombre"]
        indexes = [
            # "List active vendors for this empresa"
            models.Index(
                fields=["empresa", "activo", "nombre"],
                name="idx_proveedor_empresa_activo",
            ),
        ]

    def __str__(self):
        return self.nombre


# ─────────────────────────────────────────────────────────────────────────────
# OrdenCompra
# ─────────────────────────────────────────────────────────────────────────────

class OrdenCompra(EmpresaModel):
    """
    A purchase order sent to a Proveedor.

    State machine:
        BORRADOR → ENVIADA → RECIBIDA_PARCIAL → RECIBIDA_COMPLETA
        BORRADOR → CANCELADA
        ENVIADA  → CANCELADA

    Stock is only affected when recibir_orden() is called (transition to
    RECIBIDA_PARCIAL or RECIBIDA_COMPLETA). Creating or sending an order
    does NOT change stock.

    Partial reception:
        An order for 100 units may be received in multiple steps:
        Step 1: recibir 60 → estado = RECIBIDA_PARCIAL, creates ENTRADA(60)
        Step 2: recibir 40 → estado = RECIBIDA_COMPLETA, creates ENTRADA(40)
        Each step is atomic: all movements in a step commit together or not at all.

    numero is a free-text reference number (the empresa's own PO number).
    It is optional — some businesses don't maintain PO numbers.
    """

    proveedor = models.ForeignKey(
        Proveedor,
        on_delete=models.PROTECT,
        related_name="ordenes",
        help_text="Vendor receiving this purchase order.",
    )
    estado = models.CharField(
        max_length=30,
        choices=EstadoOrdenCompra.choices,
        default=EstadoOrdenCompra.BORRADOR,
    )
    numero = models.CharField(
        max_length=50,
        blank=True,
        help_text="Optional internal PO number for reference.",
    )
    fecha_emision = models.DateField(
        help_text="Date the order was created.",
    )
    fecha_esperada = models.DateField(
        null=True,
        blank=True,
        help_text="Expected delivery date.",
    )
    fecha_recepcion = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Actual first reception timestamp (set on first recibir_orden call).",
    )
    notas = models.TextField(blank=True)

    class Meta:
        db_table            = "inventario_orden_compra"
        verbose_name        = "Orden de Compra"
        verbose_name_plural = "Órdenes de Compra"
        ordering            = ["-fecha_emision"]
        constraints = [
            # fecha_esperada, when set, must be on or after fecha_emision.
            models.CheckConstraint(
                check=(
                    models.Q(fecha_esperada__isnull=True)
                    | models.Q(fecha_esperada__gte=models.F("fecha_emision"))
                ),
                name="check_orden_fecha_esperada_gte_emision",
            ),
        ]
        indexes = [
            # "All open orders for this empresa" (purchasing dashboard)
            models.Index(
                fields=["empresa", "estado", "-fecha_emision"],
                name="idx_orden_empresa_estado",
            ),
            # "All orders for this vendor"
            models.Index(
                fields=["empresa", "proveedor", "-fecha_emision"],
                name="idx_orden_empresa_proveedor",
            ),
        ]

    @property
    def es_editable(self) -> bool:
        """Only BORRADOR orders can be edited (items added/removed)."""
        return self.estado == EstadoOrdenCompra.BORRADOR

    @property
    def es_terminal(self) -> bool:
        """RECIBIDA_COMPLETA and CANCELADA accept no further transitions."""
        return self.estado in (
            EstadoOrdenCompra.RECIBIDA_COMPLETA,
            EstadoOrdenCompra.CANCELADA,
        )

    @property
    def esta_recibida_completamente(self) -> bool:
        """True when every line item has been fully received."""
        return all(
            d.cantidad_recibida >= d.cantidad_pedida
            for d in self.detalles.all()
        )

    def __str__(self):
        num = f" #{self.numero}" if self.numero else ""
        return f"OC{num} — {self.proveedor} ({self.get_estado_display()})"


# ─────────────────────────────────────────────────────────────────────────────
# OrdenCompraDetalle
# ─────────────────────────────────────────────────────────────────────────────

class OrdenCompraDetalle(EmpresaModel):
    """
    A single line item in a purchase order.

    Tracks both the quantity ordered and the quantity received, enabling
    partial reception tracking across multiple OrdenCompraService.recibir_orden()
    calls.

    Invariant: cantidad_recibida <= cantidad_pedida
    (Enforced by CheckConstraint; recibir_orden() also validates before accepting.)

    precio_unitario is the agreed per-unit cost with the vendor. When a
    reception creates a MovimientoStock(ENTRADA), this value is passed as
    costo_unitario for inventory valuation purposes.
    """

    orden = models.ForeignKey(
        OrdenCompra,
        on_delete=models.CASCADE,
        related_name="detalles",
    )
    producto = models.ForeignKey(
        Producto,
        on_delete=models.PROTECT,
        related_name="detalles_orden",
    )
    cantidad_pedida = models.PositiveIntegerField(
        help_text="Units ordered from the vendor.",
    )
    cantidad_recibida = models.PositiveIntegerField(
        default=0,
        help_text="Units received so far. Updated by recibir_orden().",
    )
    precio_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Agreed cost per unit. Passed to MovimientoStock.costo_unitario on reception.",
    )

    class Meta:
        db_table            = "inventario_orden_compra_detalle"
        verbose_name        = "Detalle de Orden de Compra"
        verbose_name_plural = "Detalles de Órdenes de Compra"
        constraints = [
            # cantidad_pedida must be strictly positive.
            models.CheckConstraint(
                check=models.Q(cantidad_pedida__gt=0),
                name="check_detalle_cantidad_pedida_positiva",
            ),
            # Cannot receive more than was ordered.
            models.CheckConstraint(
                check=models.Q(cantidad_recibida__lte=models.F("cantidad_pedida")),
                name="check_detalle_recibida_lte_pedida",
            ),
            # The same product must not appear twice in a single order.
            models.UniqueConstraint(
                fields=["orden", "producto"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_detalle_producto_por_orden",
            ),
        ]
        indexes = [
            # "All line items in this order" (order detail view)
            models.Index(
                fields=["orden", "producto"],
                name="idx_detalle_orden_producto",
            ),
            # "All orders containing this product" (product purchase history)
            models.Index(
                fields=["empresa", "producto"],
                name="idx_detalle_empresa_producto",
            ),
        ]

    @property
    def pendiente_recepcion(self) -> int:
        """Units ordered but not yet received."""
        return self.cantidad_pedida - self.cantidad_recibida

    @property
    def esta_completo(self) -> bool:
        """True when all ordered units have been received."""
        return self.cantidad_recibida >= self.cantidad_pedida

    def __str__(self):
        return (
            f"{self.producto} × {self.cantidad_pedida} "
            f"(recibido: {self.cantidad_recibida})"
        )
