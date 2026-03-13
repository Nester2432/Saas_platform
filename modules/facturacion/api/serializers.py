from rest_framework import serializers
from modules.facturacion.models import Factura, LineaFactura, PuntoVenta

class PuntoVentaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PuntoVenta
        fields = ["id", "codigo", "descripcion", "activo"]
        read_only_fields = ["id"]

class LineaFacturaSerializer(serializers.ModelSerializer):
    class Meta:
        model = LineaFactura
        fields = [
            "id", "producto", "descripcion", "cantidad", 
            "precio_unitario", "subtotal"
        ]

class FacturaSerializer(serializers.ModelSerializer):
    lineas = LineaFacturaSerializer(many=True, read_only=True)
    venta_numero = serializers.CharField(source="venta.numero", read_only=True)
    punto_venta_codigo = serializers.CharField(source="punto_venta.codigo", read_only=True)
    
    class Meta:
        model = Factura
        fields = [
            "id", "venta", "venta_numero", "punto_venta", "punto_venta_codigo",
            "numero", "tipo", "estado",
            "subtotal", "impuestos", "total", "moneda", "fecha_emision",
            "cae", "cae_vencimiento", "afip_resultado", "lineas",
            "created_at", "updated_at"
        ]
        read_only_fields = [
            "numero", "estado", "subtotal", "impuestos", 
            "total", "fecha_emision", "cae", "cae_vencimiento", "afip_resultado"
        ]
