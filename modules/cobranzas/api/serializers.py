from rest_framework import serializers
from modules.cobranzas.models import Pago, EstadoPago

class PagoSerializer(serializers.ModelSerializer):
    metodo_pago_nombre = serializers.CharField(source="metodo_pago.nombre", read_only=True)
    estado_display = serializers.CharField(source="get_estado_display", read_only=True)
    
    class Meta:
        model = Pago
        fields = [
            "id", "venta", "monto", "moneda", "metodo_pago", 
            "metodo_pago_nombre", "estado", "estado_display", 
            "referencia_externa", "created_at"
        ]
        read_only_fields = ["estado", "created_at"]

class RegistrarPagoSerializer(serializers.Serializer):
    venta_id = serializers.UUIDField()
    monto = serializers.DecimalField(max_digits=14, decimal_places=2)
    metodo_pago_id = serializers.UUIDField()
    moneda = serializers.CharField(max_length=3, default="ARS")
    referencia_externa = serializers.CharField(max_length=100, required=False, allow_blank=True)
