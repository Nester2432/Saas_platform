"""
demo_views.py

POST /api/v1/events/demo/full-flow/

Executes the full domain event chain in a single request:
  1. crear_cliente         → evento: cliente_creado
  2. crear_venta           → evento: venta_creada
  3. confirmar_venta       → evento: venta_confirmada
  4. marcar_como_pagada    → evento: venta_pagada
     └─ facturacion_handler → FacturaService.generar_factura_desde_venta
        └─ FacturaService  → evento: factura_emitida
           └─ notificacion_handler → NotificacionService.enviar_factura

Response includes each step's event_id and EventStore status.

⚠  PROTECTED: requires IsSuperAdmin. Use only in staging / demo environments.
"""
import logging
import time
from decimal import Decimal

from django.db import transaction
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication

class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return  # Do not enforce CSRF for demo/internal tools

from modules.events.models import EventStore
from modules.events import events

logger = logging.getLogger(__name__)

# ── Permissions ──────────────────────────────────────────────────────────────
try:
    from apps.usuarios.permissions import IsSuperAdmin
    _DEMO_PERMISSION_CLASSES = [IsSuperAdmin]
except ImportError:
    from rest_framework.permissions import IsAdminUser
    _DEMO_PERMISSION_CLASSES = [IsAdminUser]


def _get_event_status(event_name: str, empresa_id: str, after_event_id: str = None):
    """Return the most recent EventStore record for this event/empresa."""
    qs = EventStore.objects.filter(
        event_name=event_name,
        empresa_id=str(empresa_id),
    ).order_by("-created_at")
    record = qs.first()
    if record:
        return {
            "event_id": str(record.event_id),
            "store_id": str(record.id),
            "status": record.status,
            "version": record.version,
            "created_at": record.created_at.isoformat(),
            "processed_at": record.processed_at.isoformat() if record.processed_at else None,
            "retry_count": record.retry_count,
            "error_log": record.error_log,
        }
    return None


class DemoFullFlowView(APIView):
    """
    POST /api/v1/events/demo/full-flow/

    Body (JSON):
    {
        "nombre_cliente":        "Juan Demo",        # optional, default provided
        "email_cliente":         "juan@demo.com",    # optional
        "producto_descripcion":  "Suscripción Pro",  # optional
        "precio":                "100.00"             # optional, default 100.00
    }

    Returns each step with the event_id and EventStore status.
    """
    permission_classes = _DEMO_PERMISSION_CLASSES
    authentication_classes = [CsrfExemptSessionAuthentication, JWTAuthentication]

    def post(self, request):
        from apps.empresas.models import Empresa
        from apps.usuarios.models import Usuario

        user = request.user
        empresa = getattr(user, "empresa", None)

        if empresa is None:
            try:
                empresa = Empresa.objects.filter(activa=True).first()
            except Exception:
                empresa = None

        if empresa is None:
            return Response(
                {"error": "No active empresa found for this user."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = request.data
        nombre_cliente = data.get("nombre_cliente", "Demo Cliente")
        email_cliente = data.get("email_cliente")
        if not email_cliente:
            # Use timestamp to ensure uniqueness in repeated demo runs
            email_cliente = f"demo_{int(time.time())}@example.com"
        producto_desc = data.get("producto_descripcion", "Producto Demo")
        precio = Decimal(str(data.get("precio", "100.00")))

        steps = {}
        errors = []

        # ── Step 1: Crear Cliente ─────────────────────────────────────────────
        try:
            from modules.clientes.services import ClienteService
            cliente = ClienteService().crear_cliente(
                empresa=empresa,
                datos={"nombre": nombre_cliente, "email": email_cliente},
                usuario=user,
            )
            steps["1_cliente_creado"] = {
                "cliente_id": str(cliente.id),
                "nombre": nombre_cliente,
                **(_get_event_status(events.CLIENTE_CREADO, empresa.id) or {}),
            }
        except Exception as exc:
            logger.exception("Demo: Step 1 (crear_cliente) failed")
            errors.append({"step": "1_cliente_creado", "error": str(exc)})
            return Response({"steps": steps, "errors": errors}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ── Step 2: Crear Venta (borrador) ────────────────────────────────────
        try:
            from modules.ventas.services.ventas import VentaService
            from modules.ventas.models import MetodoPago

            venta = VentaService.crear_venta(
                empresa=empresa,
                cliente=cliente,
                pago_diferido=True,
                usuario=user,
            )
            VentaService.agregar_linea(
                empresa=empresa,
                venta=venta,
                descripcion=producto_desc,
                precio_unitario=precio,
                cantidad=1,
                usuario=user,
            )
            steps["2_venta_creada"] = {
                "venta_id": str(venta.id),
                "total": float(venta.total),
                **(_get_event_status(events.VENTA_CREADA, empresa.id) or {}),
            }
        except Exception as exc:
            logger.exception("Demo: Step 2 (crear_venta) failed")
            errors.append({"step": "2_venta_creada", "error": str(exc)})
            return Response({"steps": steps, "errors": errors}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ── Step 3: Confirmar Venta ───────────────────────────────────────────
        try:
            venta = VentaService.confirmar_venta(
                empresa=empresa,
                venta=venta,
                usuario=user,
            )
            steps["3_venta_confirmada"] = {
                "estado": venta.estado,
                **(_get_event_status(events.VENTA_CONFIRMADA, empresa.id) or {}),
            }
        except Exception as exc:
            logger.exception("Demo: Step 3 (confirmar_venta) failed")
            errors.append({"step": "3_venta_confirmada", "error": str(exc)})
            return Response({"steps": steps, "errors": errors}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ── Step 4: Marcar como Pagada (triggers factura + notificacion) ──────
        try:
            venta = VentaService.marcar_como_pagada(
                empresa=empresa,
                venta=venta,
                usuario=user,
            )
            steps["4_venta_pagada"] = {
                "estado": venta.estado,
                **(_get_event_status(events.VENTA_PAGADA, empresa.id) or {}),
            }
        except Exception as exc:
            logger.exception("Demo: Step 4 (marcar_como_pagada) failed")
            errors.append({"step": "4_venta_pagada", "error": str(exc)})
            return Response({"steps": steps, "errors": errors}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ── Step 5: Check factura_emitida event (generated by handler) ────────
        factura_event = _get_event_status(events.FACTURA_EMITIDA, empresa.id)
        steps["5_factura_emitida"] = factura_event or {
            "note": "Factura event not yet found (may be processing async)"
        }

        return Response({
            "message": "Demo flow completed successfully",
            "empresa_id": str(empresa.id),
            "steps": steps,
            "errors": errors,
            "note": (
                "Status shown is at request time. "
                "In async mode (EVENT_BUS_ASYNC=True), handlers run in background workers — "
                "status may evolve to PROCESSED after this response."
            ),
        }, status=status.HTTP_201_CREATED)


class DemoResourcesView(APIView):
    """
    GET /api/v1/events/demo/resources/
    List recent demo resources (clients, sales, invoices).
    """
    permission_classes = _DEMO_PERMISSION_CLASSES
    authentication_classes = [CsrfExemptSessionAuthentication, JWTAuthentication]

    def get(self, request):
        from modules.clientes.models import Cliente
        from modules.ventas.models import Venta
        from modules.facturacion.models import Factura

        user = request.user
        empresa = getattr(user, "empresa", None)

        # Fallback for demo if no company linked to user
        if not empresa:
            from apps.empresas.models import Empresa
            empresa = Empresa.objects.filter(activa=True).first()

        if not empresa:
            return Response({"error": "No active empresa found."}, status=status.HTTP_400_BAD_REQUEST)

        # Get latest 10 of each
        clientes = Cliente.objects.filter(empresa=empresa).order_by("-created_at")[:10]
        ventas = Venta.objects.filter(empresa=empresa).order_by("-created_at")[:10]
        facturas = Factura.objects.filter(empresa=empresa).order_by("-created_at")[:10]

        return Response({
            "empresa_id": str(empresa.id),
            "recent_resources": {
                "clientes": [
                    {
                        "id": str(c.id), 
                        "nombre": getattr(c, "nombre_completo", f"{c.nombre} {c.apellido}"), 
                        "email": c.email,
                        "telefono": c.telefono,
                        "created_at": c.created_at
                    } for c in clientes
                ],
                "ventas": [
                    {
                        "id": str(v.id), 
                        "numero": v.numero, 
                        "total": float(v.total), 
                        "estado": v.estado, 
                        "created_at": v.created_at
                    } for v in ventas
                ],
                "facturas": [
                    {
                        "id": str(f.id), 
                        "numero": f.numero, 
                        "total": float(f.total), 
                        "created_at": f.created_at
                    } for f in facturas
                ],
            }
        })


class DemoStatusView(APIView):
    """
    GET /api/v1/events/demo/status/
    System-wide event processing metrics.
    """
    permission_classes = _DEMO_PERMISSION_CLASSES
    authentication_classes = [CsrfExemptSessionAuthentication, JWTAuthentication]

    def get(self, request):
        from django.db.models import Count, Avg, F, ExpressionWrapper, fields
        from modules.events.models import EventStatus

        user = request.user
        empresa = getattr(user, "empresa", None)

        # Metrics for the whole system (admin view)
        qs = EventStore.objects.all()
        if empresa:
            qs = qs.filter(empresa_id=str(empresa.id))

        status_counts = qs.values("status").annotate(count=Count("id"))
        
        # Calculate avg processing time for PROCESSED events
        avg_processing = qs.filter(status=EventStatus.PROCESSED, processed_at__isnull=False).annotate(
            duration=ExpressionWrapper(
                F("processed_at") - F("created_at"),
                output_field=fields.DurationField()
            )
        ).aggregate(avg_duration=Avg("duration"))

        last_processed = qs.filter(status=EventStatus.PROCESSED).order_by("-processed_at").first()

        # Recent errors
        recent_errors = qs.filter(status__in=[EventStatus.FAILED, EventStatus.FAILED_PERMANENT]).order_by("-created_at")[:5]

        avg_duration = avg_processing.get("avg_duration")
        avg_ms = avg_duration.total_seconds() * 1000 if avg_duration else None

        return Response({
            "metrics": {
                "status_breakdown": {item["status"]: item["count"] for item in status_counts},
                "avg_processing_ms": avg_ms,
                "last_processed_at": last_processed.processed_at if last_processed else None,
            },
            "recent_errors": [
                {
                    "event_name": e.event_name,
                    "event_id": str(e.event_id),
                    "status": e.status,
                    "error": e.error_log,
                    "timestamp": e.created_at
                } for e in recent_errors
            ]
        })


class DemoActionView(APIView):
    """
    POST /api/v1/events/demo/action/
    Ejecuta una acción manual específica (cliente, venta, pago).
    """
    permission_classes = _DEMO_PERMISSION_CLASSES
    authentication_classes = [CsrfExemptSessionAuthentication, JWTAuthentication]

    def post(self, request):
        from apps.empresas.models import Empresa
        from modules.clientes.services import ClienteService
        from modules.ventas.services.ventas import VentaService
        from modules.ventas.models import Venta
        from modules.clientes.models import Cliente
        from django.core.exceptions import ValidationError

        user = request.user
        empresa = getattr(user, "empresa", None)
        if not empresa:
            empresa = Empresa.objects.filter(activa=True).first()

        if not empresa:
            return Response({"error": "No se encontró una empresa activa."}, status=status.HTTP_400_BAD_REQUEST)

        action = request.data.get("action")
        data = request.data.get("data", {})

        try:
            if action == "crear_cliente":
                # Ensure we have clean data
                nombre = str(data.get("nombre", "")).strip() or "Cliente Manual"
                raw_email = str(data.get("email", "")).strip()
                email = raw_email or f"manual_{int(time.time())}@example.com"
                
                cliente = ClienteService().crear_cliente(
                    empresa=empresa,
                    datos={"nombre": nombre, "email": email},
                    usuario=user,
                )
                return Response({
                    "message": "Cliente creado con éxito",
                    "id": str(cliente.id),
                    "nombre": nombre,
                    **(_get_event_status(events.CLIENTE_CREADO, empresa.id) or {})
                })

            elif action == "crear_venta":
                cliente_id = data.get("cliente_id")
                cliente = Cliente.objects.get(id=cliente_id, empresa=empresa)
                venta = VentaService.crear_venta(
                    empresa=empresa,
                    cliente=cliente,
                    pago_diferido=True,
                    usuario=user,
                )
                # Añadir una línea por defecto
                VentaService.agregar_linea(
                    empresa=empresa,
                    venta=venta,
                    descripcion="Producto Manual",
                    precio_unitario=Decimal("50.00"),
                    cantidad=1,
                    usuario=user,
                )
                return Response({
                    "message": "Venta (borrador) creada con éxito",
                    "id": str(venta.id),
                    **(_get_event_status(events.VENTA_CREADA, empresa.id) or {})
                })

            elif action == "confirmar_venta":
                venta_id = data.get("venta_id")
                venta = Venta.objects.get(id=venta_id, empresa=empresa)
                VentaService.confirmar_venta(venta, usuario=user)
                return Response({
                    "message": "Venta confirmada",
                    "id": str(venta.id),
                    **(_get_event_status(events.VENTA_CONFIRMADA, empresa.id) or {})
                })

            elif action == "pagar_venta":
                venta_id = data.get("venta_id")
                venta = Venta.objects.get(id=venta_id, empresa=empresa)
                VentaService.marcar_como_pagada(empresa=empresa, venta=venta, usuario=user)
                return Response({
                    "message": "Venta marcada como pagada",
                    "id": str(venta.id),
                    "note": "La factura y notificación se procesarán asíncronamente.",
                    **(_get_event_status(events.VENTA_PAGADA, empresa.id) or {})
                })

            else:
                return Response({"error": f"Acción desconocida: {action}"}, status=status.HTTP_400_BAD_REQUEST)

        except ValidationError as e:
            return Response({
                "error": "Error de validación",
                "details": e.message_dict if hasattr(e, "message_dict") else str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Error en acción manual {action}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DemoDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "events/dashboard.html"
    login_url = "/admin/login/"
    def test_func(self):
        return self.request.user.is_staff or getattr(self.request.user, "is_empresa_admin", False)
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_page"] = "dashboard"
        return context

class DemoClientesView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "events/clientes.html"
    login_url = "/admin/login/"
    def test_func(self):
        return self.request.user.is_staff or getattr(self.request.user, "is_empresa_admin", False)
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_page"] = "clientes"
        return context

class DemoVentasView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "events/ventas.html"
    login_url = "/admin/login/"
    def test_func(self):
        return self.request.user.is_staff or getattr(self.request.user, "is_empresa_admin", False)
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_page"] = "ventas"
        return context

class DemoInventarioView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "events/inventario.html"
    login_url = "/admin/login/"
    def test_func(self):
        return self.request.user.is_staff or getattr(self.request.user, "is_empresa_admin", False)
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_page"] = "inventario"
        return context

class DemoFacturacionView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "events/facturacion.html"
    login_url = "/admin/login/"
    def test_func(self):
        return self.request.user.is_staff or getattr(self.request.user, "is_empresa_admin", False)
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_page"] = "facturacion"
        return context

class DemoAgendaView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "events/agenda.html"
    login_url = "/admin/login/"
    def test_func(self):
        return self.request.user.is_staff or getattr(self.request.user, "is_empresa_admin", False)
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_page"] = "agenda"
        return context

class DemoBillingView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "events/billing.html"
    login_url = "/admin/login/"
    def test_func(self):
        return self.request.user.is_staff or getattr(self.request.user, "is_empresa_admin", False)
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_page"] = "billing"
        return context

class DemoEventosView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "events/event_log.html"
    login_url = "/admin/login/"
    def test_func(self):
        return self.request.user.is_staff or getattr(self.request.user, "is_empresa_admin", False)
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_page"] = "eventos"
        return context
