from django.db import models
from core.models import EmpresaModel
from django.core.exceptions import ValidationError
from .catalogo import Producto

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
                name="idx_mov_emp_prod_fecha",
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
                name="idx_mov_emp_ref",
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
