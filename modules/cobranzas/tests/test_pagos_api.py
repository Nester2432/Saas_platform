from decimal import Decimal
from rest_framework import status
from rest_framework.test import APITestCase
from modules.cobranzas.models import Pago, EstadoPago
from modules.ventas.models import Venta, MetodoPago
from modules.ventas.services import VentaService
from modules.inventario.tests.factories import (
    setup_producto_con_stock,
    activar_modulo,
)

from django.test import override_settings

@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class TestPagosAPI(APITestCase):
    def setUp(self):
        ctx = setup_producto_con_stock(stock_inicial=10)
        self.empresa = ctx["empresa"]
        self.admin = ctx["admin"]
        self.producto = ctx["producto"]
        
        # Activate necessary modules
        activar_modulo(self.empresa, "ventas")
        
        # Authenticate and set tenant header
        self.client.force_authenticate(user=self.admin)
        self.client.credentials(HTTP_X_EMPRESA_ID=str(self.empresa.id))
        
        self.venta = VentaService.crear_venta(self.empresa, usuario=self.admin, pago_diferido=True)
        VentaService.agregar_linea(self.empresa, self.venta, producto=self.producto, cantidad=1)
        VentaService.confirmar_venta(self.empresa, self.venta, usuario=self.admin)
        self.venta.refresh_from_db()
        
        self.metodo = MetodoPago.objects.create(
            empresa=self.empresa,
            nombre="Tarjeta",
            tipo="TARJETA"
        )
        
        self.url_base = "/api/v1/cobranzas/"

    def test_crear_pago_api(self):
        data = {
            "venta_id": str(self.venta.id),
            "monto": "50.00",
            "metodo_pago_id": str(self.metodo.id),
            "moneda": "ARS"
        }
        # We need to ensure the middleware or similar sets request.empresa.
        # In tests, we can manually set it if needed, or if the view handles it.
        # Since we use TenantQuerysetMixin, let's see how it gets empresa.
        # Usually it's from request.empresa.
        
        response = self.client.post(self.url_base, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Pago.objects.count(), 1)
        self.assertEqual(Pago.objects.first().estado, EstadoPago.PENDIENTE)

    def test_confirmar_pago_api(self):
        pago = Pago.objects.create(
            empresa=self.empresa,
            venta=self.venta,
            monto=Decimal("50"),
            metodo_pago=self.metodo,
            estado=EstadoPago.PENDIENTE
        )
        url = f"{self.url_base}{pago.id}/confirmar/"
        response = self.client.post(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pago.refresh_from_db()
        self.assertEqual(pago.estado, EstadoPago.CONFIRMADO)
        
        # Check sales record
        self.venta.refresh_from_db()
        self.assertEqual(self.venta.pagos.count(), 1)

    def test_sobrepago_api_error(self):
        pago = Pago.objects.create(
            empresa=self.empresa,
            venta=self.venta,
            monto=self.venta.total + Decimal("100"),
            metodo_pago=self.metodo,
            estado=EstadoPago.PENDIENTE
        )
        url = f"{self.url_base}{pago.id}/confirmar/"
        response = self.client.post(url)
        
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "sobrepago_error")

    def test_listar_pagos_por_venta(self):
        Pago.objects.create(
            empresa=self.empresa, venta=self.venta, 
            monto=Decimal("10"), metodo_pago=self.metodo
        )
        response = self.client.get(f"{self.url_base}?venta={self.venta.id}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # DRF DefaultRouter uses pagination or list
        if "results" in response.data:
            self.assertEqual(len(response.data["results"]), 1)
        else:
            self.assertEqual(len(response.data), 1)
