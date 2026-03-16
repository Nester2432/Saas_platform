from rest_framework import serializers
from apps.usuarios.models import Usuario

class UserSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for fetching user details.
    """
    class Meta:
        model = Usuario
        fields = [
            "id", "email", "nombre", "apellido", "rol",
            "is_active", "created_at", "updated_at"
        ]
        read_only_fields = fields


class UserCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating a new user within the current Empresa.
    """
    password = serializers.CharField(write_only=True, required=True)

    class Meta:
        model = Usuario
        fields = [
            "id", "email", "password", "nombre", "apellido", "rol",
            "is_active"
        ]

    def create(self, validated_data):
        # Extract empresa from context or validated_data (if injected by save())
        empresa = validated_data.pop("empresa", None) or self.context["request"].empresa
        password = validated_data.pop("password")
        
        # We manually construct the user to ensure it's bound strictly to the tenant
        user = Usuario.objects.create_user(
            email=validated_data.pop("email"),
            empresa=empresa,
            password=password,
            **validated_data
        )
        return user


class UserUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer for updating an existing user.
    Prevents Modification of email to avoid constraint collisions.
    """
    class Meta:
        model = Usuario
        fields = ["nombre", "apellido", "rol", "is_active"]
