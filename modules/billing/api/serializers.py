from rest_framework import serializers
from modules.billing.models import Plan, Suscripcion, UsoMensual

class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = [
            "id", "nombre", "descripcion", 
            "precio_mensual", "precio_anual",
            "max_usuarios", "max_clientes", "max_productos",
            "activo"
        ]

class SuscripcionSerializer(serializers.ModelSerializer):
    plan_nombre = serializers.ReadOnlyField(source="plan.nombre")
    
    class Meta:
        model = Suscripcion
        fields = [
            "id", "plan", "plan_nombre", "estado", 
            "fecha_inicio", "fecha_fin", 
            "periodo_facturacion", "auto_renovar"
        ]

class UsoMensualSerializer(serializers.ModelSerializer):
    class Meta:
        model = UsoMensual
        fields = ["mes", "usuarios_creados", "productos_creados", "ventas_creadas"]
