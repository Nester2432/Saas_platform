from django.db import models
from django.db.models import UniqueConstraint
from core.models import EmpresaModel
from modules.ventas.models import Venta
from modules.inventario.models import Producto

class EstadoFactura(models.TextChoices):
    BORRADOR = "BORRADOR", "Borrador"
    EMITIDA  = "EMITIDA", "Emitida"
    ANULADA  = "ANULADA", "Anulada"

class TipoComprobante(models.TextChoices):
    A = "A", "Factura A"
    B = "B", "Factura B"
    C = "C", "Factura C"

class PuntoVenta(EmpresaModel):
    codigo = models.CharField(
        max_length=4, 
        default="0001",
        help_text="Código de 4 dígitos (ej: 0001)"
    )
    descripcion = models.CharField(max_length=100, default="Punto de venta principal")
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Punto de Venta"
        verbose_name_plural = "Puntos de Venta"
        constraints = [
            UniqueConstraint(
                fields=["empresa", "codigo"],
                name="uniq_punto_venta_empresa_codigo"
            )
        ]

    def __str__(self):
        return f"{self.codigo} - {self.descripcion}"

class SecuenciaComprobante(EmpresaModel):
    punto_venta = models.ForeignKey(
        PuntoVenta, 
        on_delete=models.CASCADE, 
        related_name="secuenciadores"
    )
    tipo_comprobante = models.CharField(
        max_length=2, 
        choices=TipoComprobante.choices
    )
    ultimo_numero = models.PositiveIntegerField(
        default=0,
        help_text="Último número de comprobante utilizado para este tipo y punto de venta."
    )

    class Meta:
        verbose_name = "Secuencia de Comprobante"
        verbose_name_plural = "Secuencias de Comprobantes"
        constraints = [
            UniqueConstraint(
                fields=["empresa", "punto_venta", "tipo_comprobante"],
                name="uniq_secuencia_comprobante"
            )
        ]

    def __str__(self):
        return f"{self.punto_venta.codigo} | {self.tipo_comprobante} | {self.ultimo_numero}"

class Factura(EmpresaModel):
    venta = models.ForeignKey(
        Venta, 
        on_delete=models.PROTECT, 
        related_name="facturas"
    )
    punto_venta = models.ForeignKey(
        PuntoVenta,
        on_delete=models.PROTECT,
        related_name="facturas",
        null=True,
        blank=True
    )
    numero_secuencial = models.PositiveIntegerField(null=True, blank=True)
    numero = models.CharField(
        max_length=20, 
        blank=True,
        help_text="Formato: 0001-00000001"
    )
    tipo = models.CharField(
        max_length=2, 
        choices=TipoComprobante.choices,
        default=TipoComprobante.B
    )
    estado = models.CharField(
        max_length=20, 
        choices=EstadoFactura.choices, 
        default=EstadoFactura.BORRADOR
    )
    
    fecha_emision = models.DateTimeField(null=True, blank=True)
    
    # Totals (snapshots)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    impuestos = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    moneda = models.CharField(max_length=3, default="ARS")
    
    # Campos AFIP / Facturación Electrónica (Preparación)
    cae = models.CharField(max_length=30, blank=True, null=True)
    cae_vencimiento = models.DateField(null=True, blank=True)
    afip_resultado = models.CharField(max_length=20, blank=True, null=True)
    afip_xml_request = models.TextField(blank=True, null=True)
    afip_xml_response = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = "Factura"
        verbose_name_plural = "Facturas"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tipo} {self.numero or 'BORRADOR'} ({self.venta})"

class LineaFactura(EmpresaModel):
    factura = models.ForeignKey(
        Factura, 
        on_delete=models.CASCADE, 
        related_name="lineas"
    )
    producto = models.ForeignKey(
        Producto, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True
    )
    descripcion = models.CharField(max_length=255)
    cantidad = models.DecimalField(max_digits=12, decimal_places=2)
    precio_unitario = models.DecimalField(max_digits=12, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = "Línea de Factura"
        verbose_name_plural = "Líneas de Factura"

    def __str__(self):
        return f"{self.descripcion} x {self.cantidad}"
