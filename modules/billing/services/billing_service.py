import logging
from datetime import date, timedelta
from django.utils import timezone
from django.db import transaction
from django.core.exceptions import ValidationError
from modules.billing.models import Plan, Suscripcion, UsoMensual, EstadoSuscripcion, PeriodoFacturacion
from modules.events.event_bus import EventBus
from modules.events import events

logger = logging.getLogger(__name__)

class BillingService:
    
    @staticmethod
    def create_subscription(empresa, plan: Plan, periodo=PeriodoFacturacion.MONTHLY, is_trial=False, usuario=None):
        """
        Creates a new subscription for a company.
        If is_trial=True, sets status to TRIAL and sets 14 days duration.
        """
        with transaction.atomic():
            # Deactivate existing active/trial subscriptions
            Suscripcion.objects.filter(
                empresa=empresa, 
                estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]
            ).update(estado=EstadoSuscripcion.CANCELED)

            fecha_inicio = timezone.now().date()
            if is_trial:
                fecha_fin = fecha_inicio + timedelta(days=14)
                estado = EstadoSuscripcion.TRIAL
            else:
                dias = 365 if periodo == PeriodoFacturacion.ANNUAL else 30
                fecha_fin = fecha_inicio + timedelta(days=dias)
                estado = EstadoSuscripcion.ACTIVE

            suscripcion = Suscripcion.objects.create(
                empresa=empresa,
                plan=plan,
                estado=estado,
                fecha_inicio=fecha_inicio,
                fecha_fin=fecha_fin,
                periodo_facturacion=periodo,
                created_by=usuario,
                updated_by=usuario
            )

            # Emit events
            event_name = events.TRIAL_STARTED if is_trial else events.SUBSCRIPTION_CREATED
            EventBus.publish(
                event_name,
                empresa_id=empresa.id,
                usuario_id=usuario.id if usuario else None,
                plan_id=plan.id,
                plan_nombre=plan.nombre,
                fecha_fin=str(fecha_fin)
            )
            
            return suscripcion

    @staticmethod
    def cancel_subscription(empresa, usuario=None):
        """
        Cancels the current active/trial subscription.
        """
        suscripcion = Suscripcion.objects.filter(
            empresa=empresa, 
            estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]
        ).first()

        if suscripcion:
            suscripcion.estado = EstadoSuscripcion.CANCELED
            suscripcion.updated_by = usuario
            suscripcion.save(update_fields=["estado", "updated_by", "updated_at"])

            EventBus.publish(
                events.SUBSCRIPTION_CANCELED,
                empresa_id=empresa.id,
                usuario_id=usuario.id if usuario else None,
                plan_id=suscripcion.plan.id
            )
            return True
        return False

    @staticmethod
    def upgrade_plan(empresa, nuevo_plan: Plan, periodo=PeriodoFacturacion.MONTHLY, usuario=None):
        """
        Upgrades or changes the plan for a company.
        """
        with transaction.atomic():
            suscripcion_anterior = Suscripcion.objects.filter(
                empresa=empresa, 
                estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]
            ).first()

            nueva_suscripcion = BillingService.create_subscription(
                empresa=empresa,
                plan=nuevo_plan,
                periodo=periodo,
                is_trial=False,
                usuario=usuario
            )

            EventBus.publish(
                events.SUBSCRIPTION_UPGRADED,
                empresa_id=empresa.id,
                usuario_id=usuario.id if usuario else None,
                plan_anterior=suscripcion_anterior.plan.nombre if suscripcion_anterior else "None",
                plan_nuevo=nuevo_plan.nombre
            )
            return nueva_suscripcion

    @staticmethod
    def verificar_suscripcion_activa(empresa):
        """
        Valida si la empresa tiene una suscripción válida (ACTIVE o TRIAL) y no expirada.
        """
        if not empresa: return

        suscripcion = Suscripcion.objects.filter(
            empresa=empresa,
            estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]
        ).first()

        if not suscripcion:
            raise ValidationError("La empresa no tiene una suscripción activa.")

        if suscripcion.fecha_fin and suscripcion.fecha_fin < timezone.now().date():
            raise ValidationError(f"Su suscripción ({suscripcion.get_estado_display()}) ha expirado.")

    @staticmethod
    def check_plan_limits(empresa, recurso: str):
        """
        Validates if the company can create a new resource.
        Raises ValidationError if limit is exceeded.
        """
        if not empresa: return

        suscripcion = Suscripcion.objects.filter(
            empresa=empresa,
            estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]
        ).select_related("plan").first()

        if not suscripcion:
            raise ValidationError("La empresa no tiene una suscripción activa.")

        # Check if trial/subscription is expired
        if suscripcion.fecha_fin and suscripcion.fecha_fin < timezone.now().date():
            raise ValidationError(f"Su suscripción ({suscripcion.estado}) ha expirado el {suscripcion.fecha_fin}.")

        plan = suscripcion.plan
        limit_attr = f"max_{recurso}"
        limit = getattr(plan, limit_attr, None)

        if limit is None:
            return  # Unlimited

        usage = BillingService.get_current_usage(empresa, recurso)
        
        if usage >= limit:
            raise ValidationError(
                f"Límite de {recurso} alcanzado para el plan {plan.nombre} ({usage}/{limit}). "
                "Por favor, actualice su plan."
            )

    @staticmethod
    def get_current_usage(empresa, recurso: str) -> int:
        """
        Calculates real-time usage for a resource.
        """
        if recurso == "usuarios":
            from apps.usuarios.models import Usuario
            return Usuario.objects.filter(empresa=empresa).count()
        
        if recurso == "clientes":
            from modules.clientes.models import Cliente
            return Cliente.objects.filter(empresa=empresa).count()
        
        if recurso == "productos":
            from modules.inventario.models import Producto
            return Producto.objects.filter(empresa=empresa).count()
            
        if recurso == "ventas":
            from modules.ventas.models import Venta
            return Venta.objects.filter(empresa=empresa).count()

        return 0

    @staticmethod
    def register_usage(empresa, recurso: str, cantidad: int = 1):
        """
        Registers usage for a resource. 
        In this implementation, most usage is calculated real-time by get_current_usage,
        but we can use this for specific metrics or logging if needed.
        """
        logger.info(f"Usage registered for {recurso} in empresa {empresa.id}: +{cantidad}")
        # In the future, this could update a cache or a separate Usage model for performance.

    registrar_uso = register_usage

    @staticmethod
    def get_or_create_usage_record(empresa):
        """Helper for monthly tracking if needed (Legacy support)"""
        today = date.today()
        primer_dia_mes = date(today.year, today.month, 1)
        uso, _ = UsoMensual.objects.get_or_create(empresa=empresa, mes=primer_dia_mes)
        return uso

    # Legacy method names for compatibility if needed
    obtener_plan_empresa = staticmethod(lambda e: getattr(Suscripcion.objects.filter(empresa=e, estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]).select_related("plan").first(), 'plan', None))
    verificar_limite = check_plan_limits
