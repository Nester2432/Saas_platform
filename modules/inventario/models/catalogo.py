from django.db import models
from core.models import EmpresaModel

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

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        if is_new:
            from modules.billing.services.billing_service import BillingService
            BillingService.check_plan_limits(self.empresa, "productos")

        super().save(*args, **kwargs)
        if is_new:
            from modules.events.event_bus import EventBus
            from modules.events import events
            EventBus.publish(
                events.PRODUCTO_CREADO,
                empresa_id=self.empresa_id,
                usuario_id=str(self.created_by_id) if self.created_by_id else None,
                recurso="producto",
                recurso_id=self.id,
                nombre=self.nombre
            )
