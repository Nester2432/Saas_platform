"""
modules/clientes/api/serializers_crm.py

Dedicated serializers for the CRM aggregation layer.
Designed to avoid circular dependencies between modules (clientes, ventas, turnos, facturacion).
"""

from rest_framework import serializers
from modules.clientes.models import Cliente, HistorialCliente
from modules.clientes.api.serializers import EtiquetaClienteSerializer

# --- Small Nested Resumen Serializers ---

class VentaResumenSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    numero = serializers.CharField()
    fecha = serializers.DateTimeField(source='created_at')
    total = serializers.DecimalField(max_digits=12, decimal_places=2)
    estado = serializers.CharField()
    # Mocking items count if not easily available in first pass, or fetching from line count
    cantidad_items = serializers.SerializerMethodField()

    def get_cantidad_items(self, obj):
        return obj.lineas.count() if hasattr(obj, 'lineas') else 0

class TurnoResumenSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    fecha = serializers.DateTimeField(source='fecha_inicio')
    estado = serializers.CharField()
    servicio_nombre = serializers.CharField(source='servicio.nombre', read_only=True)
    profesional_nombre = serializers.CharField(source='profesional.nombre_completo', read_only=True)

class FacturaResumenSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    numero = serializers.CharField()
    fecha = serializers.DateTimeField(source='created_at')
    total = serializers.DecimalField(max_digits=12, decimal_places=2)
    estado = serializers.CharField()

class ActividadSerializer(serializers.ModelSerializer):
    tipo_display = serializers.CharField(source='get_tipo_evento_display', read_only=True)
    class Meta:
        model = HistorialCliente
        fields = ['id', 'tipo_evento', 'tipo_display', 'descripcion', 'created_at', 'metadata']

# --- Main CRM Serializers ---

class ContactoListSerializer(serializers.ModelSerializer):
    total_ventas = serializers.IntegerField(read_only=True)
    total_turnos = serializers.IntegerField(read_only=True)
    ultima_interaccion = serializers.DateTimeField(read_only=True)
    etiquetas = EtiquetaClienteSerializer(many=True, read_only=True)
    fecha_alta = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = Cliente
        fields = [
            'id', 'nombre', 'apellido', 'email', 'telefono', 
            'activo', 'fecha_alta', 'total_ventas', 'total_turnos', 
            'ultima_interaccion', 'etiquetas'
        ]

class Contacto360Serializer(serializers.Serializer):
    """
    Assembles the full CRM 360 profile from aggregated data.
    Input object is a dict returned by get_contacto_360 selector.
    """
    cliente = ContactoListSerializer()
    ventas = VentaResumenSerializer(many=True)
    turnos = TurnoResumenSerializer(many=True)
    facturas = FacturaResumenSerializer(many=True)
    actividad = ActividadSerializer(many=True)
