from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.empresas.models import Empresa
from modules.facturacion.models import PuntoVenta

@receiver(post_save, sender=Empresa)
def crear_punto_venta_inicial(sender, instance, created, **kwargs):
    """
    Automáticamente crea el Punto de Venta '0001' cuando se registra una nueva empresa.
    Esto permite que el sistema de facturación esté listo para emitir comprobantes
    desde el primer momento.
    """
    if created:
        PuntoVenta.objects.get_or_create(
            empresa=instance,
            codigo="0001",
            defaults={
                "descripcion": "Punto de venta principal",
                "activo": True
            }
        )
