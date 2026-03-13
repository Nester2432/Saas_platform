"""
modules/ventas/tests/test_venta_items.py

Integration tests for the VentaItem workflow verifying:
1. VENDEDOR can add items via POST /ventas/{id}/items/
2. Stock reduces correctly when venta is confirmed
3. Stock cannot go below zero (StockInsuficienteError)
"""
from decimal import Decimal

from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.usuarios.models import Usuario
from apps.usuarios.auth.serializers import get_tokens_for_user
from modules.ventas.tests.factories import (
    make_empresa,
    make_metodo_pago,
    make_producto_con_stock,
    make_venta_borrador,
)
from modules.ventas.models import Venta, EstadoVenta
from modules.inventario.models import Producto


def _make_user(empresa, rol, email):
    return Usuario.objects.create_user(
        email=email,
        password="pass",
        empresa=empresa,
        rol=rol,
        nombre="Test",
        apellido="User",
    )


class VentaItemsEndpointTest(APITestCase):
    """Test the /items/ alias endpoint and stock reduction."""

    def setUp(self):
        self.empresa = make_empresa(nombre="Empresa Ventas Test")
        self.vendedor = _make_user(self.empresa, Usuario.RolUsuario.VENDEDOR, "vendedor_venta@test.com")
        self.metodo_pago = make_metodo_pago(self.empresa)
        # Create product with 10 units of stock
        self.producto = make_producto_con_stock(empresa=self.empresa, stock=10)

        # Activate 'ventas' module for this empresa
        from apps.modulos.models import Modulo, EmpresaModulo
        modulo_ventas, _ = Modulo.objects.get_or_create(codigo="ventas", defaults={"nombre": "Ventas"})
        EmpresaModulo.objects.create(empresa=self.empresa, modulo=modulo_ventas, activo=True)

        tokens = get_tokens_for_user(self.vendedor)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        # Create a BORRADOR venta directly via the service
        self.venta = make_venta_borrador(empresa=self.empresa, usuario=self.vendedor)
        self.venta_id = str(self.venta.id)

    def test_vendedor_can_add_item_via_items_endpoint(self):
        """POST /ventas/{id}/items/ should add a line item successfully."""
        url = reverse("venta-items", args=[self.venta_id])
        payload = {
            "producto_id": str(self.producto.id),
            "cantidad": 3,
            "descuento": "0.00",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # One line item should now be present
        self.assertEqual(len(response.data["lineas"]), 1)
        self.assertEqual(response.data["lineas"][0]["cantidad"], 3)

    def test_stock_reduces_after_venta_confirmed(self):
        """Confirming a venta with 5 units should reduce stock from 10 to 5."""
        stock_before = Producto.objects.get(id=self.producto.id).stock_actual

        # Add item: 5 units
        items_url = reverse("venta-items", args=[self.venta_id])
        self.client.post(items_url, {
            "producto_id": str(self.producto.id),
            "cantidad": 5,
            "descuento": "0.00",
        })

        # Confirm with payment
        confirmar_url = reverse("venta-confirmar", args=[self.venta_id])
        precio = Producto.objects.get(id=self.producto.id).precio_venta or Decimal("100.00")
        total = precio * 5

        response = self.client.post(confirmar_url, {
            "pagos": [{
                "metodo_pago_id": str(self.metodo_pago.id),
                "monto": str(total),
            }]
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify stock reduced
        stock_after = Producto.objects.get(id=self.producto.id).stock_actual
        self.assertEqual(stock_after, stock_before - 5)

    def test_stock_cannot_go_negative(self):
        """Trying to sell more than available stock should return 409."""
        # Add item: 999 units (way more than the 10 in stock)
        items_url = reverse("venta-items", args=[self.venta_id])
        self.client.post(items_url, {
            "producto_id": str(self.producto.id),
            "cantidad": 999,
            "descuento": "0.00",
        })

        # Try to confirm — should fail with 409
        confirmar_url = reverse("venta-confirmar", args=[self.venta_id])
        precio = Producto.objects.get(id=self.producto.id).precio_venta or Decimal("100.00")
        response = self.client.post(confirmar_url, {
            "pagos": [{
                "metodo_pago_id": str(self.metodo_pago.id),
                "monto": str(precio * 999),
            }]
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        # Confirm stock has NOT changed
        stock_after = Producto.objects.get(id=self.producto.id).stock_actual
        self.assertEqual(stock_after, 10)
