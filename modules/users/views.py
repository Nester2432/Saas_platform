from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken

from core.permissions.base import IsTenantAuthenticated
from modules.users.permissions import IsAdmin
from modules.users.serializers import UserSerializer, UserCreateSerializer, UserUpdateSerializer
from apps.usuarios.models import Usuario


class AuthViewSet(viewsets.ViewSet):
    """
    Authentication convenience endpoints mapped cleanly alongside JWT.
    """
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=["post"])
    def logout(self, request):
        """
        Blacklists the refresh token.
        """
        try:
            refresh_token = request.data["refresh"]
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response(status=status.HTTP_205_RESET_CONTENT)
        except Exception:
            return Response(status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], permission_classes=[IsTenantAuthenticated])
    def me(self, request):
        """
        Returns the data of the currently authenticated user.
        """
        serializer = UserSerializer(request.user)
        return Response(serializer.data)


class UserViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows users to be viewed or edited.
    Strictly isolated to the current tenant's (`request.empresa`) scope.
    """
    permission_classes = [IsTenantAuthenticated, IsAdmin]
    
    def get_queryset(self):
        # The base TenantMiddleware & base User object does not have SoftDeleteTenantManager yet,
        # but we can manually enforce the isolation here easily.
        return Usuario.objects.filter(empresa=self.request.empresa).order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        elif self.action in ["update", "partial_update"]:
            return UserUpdateSerializer
        return UserSerializer

    def perform_create(self, serializer):
        from modules.billing.services.billing_service import BillingService
        # Check user limit before creating
        BillingService.check_plan_limits(self.request.empresa, "usuarios")
        serializer.save(empresa=self.request.empresa)
