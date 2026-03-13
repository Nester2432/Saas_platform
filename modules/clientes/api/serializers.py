"""
modules/clientes/serializers.py

Serializer design decisions:

1. Read vs Write serializers are split where their shape differs significantly.
   - ClienteSerializer: full representation for GET responses (nested etiquetas, conteos)
   - ClienteCreateSerializer: flat input for POST/PUT (etiqueta IDs, no nesting)

2. empresa is NEVER accepted from request body — it is injected by TenantQuerysetMixin
   in perform_create(). Accepting it from input would be a tenant-isolation vulnerability.

3. created_by, updated_by, deleted_at are read-only and never writable from the API.

4. Nested reads use select_related/prefetch_related in the ViewSet queryset —
   serializers assume the data is already prefetched.
"""

from rest_framework import serializers

from modules.clientes.models import (
    Cliente,
    EtiquetaCliente,
    ClienteEtiqueta,
    NotaCliente,
    HistorialCliente,
)


# ---------------------------------------------------------------------------
# Etiqueta
# ---------------------------------------------------------------------------

class EtiquetaClienteSerializer(serializers.ModelSerializer):
    """Full representation of a tag. Used for list/detail and nested reads."""

    class Meta:
        model = EtiquetaCliente
        fields = ["id", "nombre", "color", "created_at"]
        read_only_fields = ["id", "created_at"]


class EtiquetaClienteCreateSerializer(serializers.ModelSerializer):
    """Input serializer for creating/updating a tag."""

    class Meta:
        model = EtiquetaCliente
        fields = ["nombre", "color"]

    def validate_nombre(self, value):
        return value.strip()


# ---------------------------------------------------------------------------
# Nota
# ---------------------------------------------------------------------------

class NotaClienteSerializer(serializers.ModelSerializer):
    """
    Serializer for reading notes. Includes author name for display.
    """
    autor = serializers.SerializerMethodField()

    class Meta:
        model = NotaCliente
        fields = ["id", "contenido", "autor", "created_at"]
        read_only_fields = ["id", "autor", "created_at"]

    def get_autor(self, obj):
        if obj.created_by_id:
            return getattr(obj.created_by, "nombre_completo", str(obj.created_by_id))
        return None


class NotaClienteCreateSerializer(serializers.Serializer):
    """
    Input for POST /clientes/{id}/notas/

    Intentionally a plain Serializer (not ModelSerializer) because
    creation goes through ClienteService.agregar_nota(), not .save() directly.
    """
    contenido = serializers.CharField(min_length=1, max_length=5000)

    def validate_contenido(self, value):
        return value.strip()


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------

class HistorialClienteSerializer(serializers.ModelSerializer):
    """Read-only serializer for history events."""

    tipo_evento_display = serializers.CharField(
        source="get_tipo_evento_display", read_only=True
    )
    autor = serializers.SerializerMethodField()

    class Meta:
        model = HistorialCliente
        fields = [
            "id", "tipo_evento", "tipo_evento_display",
            "descripcion", "metadata", "autor", "created_at",
        ]
        read_only_fields = fields

    def get_autor(self, obj):
        if obj.created_by_id:
            return getattr(obj.created_by, "nombre_completo", str(obj.created_by_id))
        return None


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

class ClienteSerializer(serializers.ModelSerializer):
    """
    Full read representation of a Cliente.

    Includes:
    - Nested etiquetas (prefetched by ViewSet)
    - Counts for notas and historial entries
    - Computed fields: nombre_completo

    Used for: GET /clientes/, GET /clientes/{id}/
    """

    etiquetas = EtiquetaClienteSerializer(many=True, read_only=True)
    nombre_completo = serializers.CharField(read_only=True)

    class Meta:
        model = Cliente
        fields = [
            "id",
            "nombre",
            "apellido",
            "nombre_completo",
            "email",
            "telefono",
            "fecha_nacimiento",
            "notas",
            "activo",
            "etiquetas",
            "metadata",
            "etiquetas",
            "metadata",
            "created_at",
            "updated_at",
            "created_by",
        ]
        read_only_fields = [
            "id", "nombre_completo", "etiquetas",
            "created_at", "updated_at", "created_by",
        ]



class ClienteCreateSerializer(serializers.ModelSerializer):
    """
    Input serializer for creating a new Cliente.

    - empresa is excluded (injected by TenantQuerysetMixin)
    - etiqueta_ids is accepted for bulk tag assignment on creation
    - Validation goes through ClienteService (not .save()) so this
      serializer's .save() is never called directly

    Used for: POST /clientes/
    """

    etiqueta_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="Optional list of EtiquetaCliente UUIDs to assign on creation."
    )

    class Meta:
        model = Cliente
        fields = [
            "nombre",
            "apellido",
            "email",
            "telefono",
            "fecha_nacimiento",
            "notas",
            "activo",
            "metadata",
            "etiqueta_ids",
        ]

    def validate_nombre(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("El nombre no puede estar vacío.")
        return value

    def validate_email(self, value):
        return value.strip().lower()


class ClienteUpdateSerializer(serializers.ModelSerializer):
    """
    Input serializer for partial updates (PATCH /clientes/{id}/).

    All fields optional — only provided fields are updated.
    empresa is always excluded.
    """

    class Meta:
        model = Cliente
        fields = [
            "nombre",
            "apellido",
            "email",
            "telefono",
            "fecha_nacimiento",
            "notas",
            "activo",
            "metadata",
        ]

    def validate_email(self, value):
        return value.strip().lower() if value else value


class AgregarEtiquetaSerializer(serializers.Serializer):
    """
    Input for POST /clientes/{id}/etiquetas/
    Accepts a single etiqueta_id to assign.
    """
    etiqueta_id = serializers.UUIDField()
