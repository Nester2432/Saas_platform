from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.authentication import SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication

class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return
from .models import EventStore
from .serializers import EventStoreSerializer
from .event_bus import EventBus

class EventStoreViewSet(viewsets.ReadOnlyModelViewSet):
    """
    SuperAdmin API to monitor and replay system events.
    """
    queryset = EventStore.objects.all()
    serializer_class = EventStoreSerializer
    permission_classes = [permissions.IsAdminUser]
    authentication_classes = [CsrfExemptSessionAuthentication, JWTAuthentication]

    def get_queryset(self):
        queryset = EventStore.objects.all()
        event_name = self.request.query_params.get("event_name")
        status = self.request.query_params.get("status")
        empresa_id = self.request.query_params.get("empresa_id")

        if event_name:
            queryset = queryset.filter(event_name=event_name)
        if status:
            queryset = queryset.filter(status=status)
        if empresa_id:
            queryset = queryset.filter(empresa_id=empresa_id)
        
        return queryset

    @action(detail=False, methods=["post"])
    def replay_failed(self, request):
        """
        Triggers a manual replay of all events in FAILED state.
        """
        count = EventBus.replay_failed_events()
        return Response(
            {"message": f"Replay triggered for {count} events."},
            status=status.HTTP_200_OK
        )
