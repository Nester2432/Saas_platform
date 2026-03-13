from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from core.mixins import TenantQuerysetMixin, AuditLogMixin
from rest_framework.permissions import IsAuthenticated
from core.permissions import IsEmpresaAdmin
from modules.billing.models import Plan, Suscripcion, UsoMensual, EstadoSuscripcion
from modules.billing.api.serializers import PlanSerializer, SuscripcionSerializer, UsoMensualSerializer
from modules.billing.services.billing_service import BillingService
from modules.events.event_bus import EventBus
from modules.events import events

class PlanViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Public plans list.
    """
    queryset = Plan.objects.filter(activo=True)
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated]

    def actual(self, request):
        """Returns the current active subscription."""
        suscripcion = self.get_queryset().filter(estado="ACTIVA").first()
        if not suscripcion:
            return Response({"detail": "No tiene una suscripción activa."}, status=status.HTTP_404_NOT_FOUND)
        serializer = self.get_serializer(suscripcion)
        return Response(serializer.data)

    @action(detail=False, methods=["post"], url_path="cambiar-plan")
    def cambiar_plan(self, request):
        """
        Request a plan change via Stripe Checkout.
        """
        plan_id = request.data.get("plan_id")
        if not plan_id:
            return Response({"detail": "Debe proporcionar un plan_id."}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            from modules.payments.services import StripeService
            plan = Plan.objects.get(id=plan_id, activo=True)
            
            # Crear sesión de checkout
            # URLs de retorno (simplificadas para el demo)
            success_url = request.build_absolute_uri('/') + "?payment=success"
            cancel_url = request.build_absolute_uri('/') + "?payment=cancel"
            
            empresa = getattr(request, "empresa", None)
            session = StripeService.create_checkout_session(
                empresa=empresa,
                plan=plan,
                success_url=success_url,
                cancel_url=cancel_url
            )
            
            # Actualizar suscripción actual a PENDING_PAYMENT o registrar intento
            suscripcion = Suscripcion.objects.filter(
                empresa=empresa,
                estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]
            ).first()
            
            if suscripcion:
                suscripcion.estado = EstadoSuscripcion.PENDING_PAYMENT
                suscripcion.save(update_fields=['estado'])
            
            return Response({
                "checkout_url": session.url,
                "session_id": session.id
            })
            
        except Plan.DoesNotExist:
            return Response({"detail": "Plan no encontrado o inactivo."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"])
    def suspender(self, request, pk=None):
        """Suspends the subscription (SuperAdmin only)."""
        if not request.user.is_superuser:
            return Response({"detail": "Solo los SuperAdmins pueden suspender suscripciones."}, status=status.HTTP_403_FORBIDDEN)
        
        suscripcion = self.get_object()
        suscripcion.estado = EstadoSuscripcion.SUSPENDIDA
        suscripcion.save(update_fields=["estado"])
        
        EventBus.publish(
            events.SUSCRIPCION_SUSPENDIDA,
            empresa_id=suscripcion.empresa_id,
            usuario_id=request.user.id,
            recurso="suscripcion",
            recurso_id=suscripcion.id
        )
        
        return Response({"status": "suscripcion suspendida"})

    @action(detail=True, methods=["post"])
    def reactivar(self, request, pk=None):
        """Reactivates the subscription (SuperAdmin only)."""
        if not request.user.is_superuser:
            return Response({"detail": "Solo los SuperAdmins pueden reactivar suscripciones."}, status=status.HTTP_403_FORBIDDEN)
            
        suscripcion = self.get_object()
        suscripcion.estado = EstadoSuscripcion.ACTIVA
        suscripcion.save(update_fields=["estado"])
        
        EventBus.publish(
            events.SUSCRIPCION_REACTIVADA,
            empresa_id=suscripcion.empresa_id,
            usuario_id=request.user.id,
            recurso="suscripcion",
            recurso_id=suscripcion.id
        )
        
        return Response({"status": "suscripcion reactivada"})

class BillingViewSet(TenantQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for checking usage stats vs limits.
    """
    queryset = UsoMensual.objects.all()
    serializer_class = UsoMensualSerializer

    def list(self, request, *args, **kwargs):
        uso = BillingService._get_or_create_uso_mensual(request.empresa)
        plan = BillingService.obtener_plan_empresa(request.empresa)
        
        data = {
            "mes": uso.mes.strftime("%Y-%m"),
            "plan_nombre": plan.nombre if plan else "Ninguno",
            "uso": {
                "ventas": {
                    "actual": uso.ventas_creadas,
                    "limite": plan.limite_ventas_mes if plan else 0
                },
                "productos": {
                    "actual": BillingService.obtener_uso_actual(request.empresa, "productos"),
                    "limite": plan.limite_productos if plan else 0
                },
                "usuarios": {
                    "actual": BillingService.obtener_uso_actual(request.empresa, "usuarios"),
                    "limite": plan.limite_usuarios if plan else 0
                }
            }
        }
        return Response(data)
