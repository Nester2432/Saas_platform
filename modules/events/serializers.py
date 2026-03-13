from rest_framework import serializers
from .models import EventStore

class EventStoreSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventStore
        fields = [
            "id", "event_name", "event_id", "empresa_id", 
            "usuario_id", "status", "retry_count", 
            "created_at", "processed_at", "error_log", "payload"
        ]
        read_only_fields = fields
