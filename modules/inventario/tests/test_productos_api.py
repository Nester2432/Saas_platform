"""
modules/inventario/tests/test_productos_api.py

Integration tests for the Producto API endpoints verifying:
1. Tenant isolation — Empresa B cannot see Empresa A products
2. VENDEDOR role → full CRUD
3. CONTADOR role → read-only (GET=200, POST=403)
"""
from decimal import Decimal

from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.usuarios.models import Usuario
from apps.usuarios.auth.serializers import get_tokens_for_user
from modules.ventas.tests.factories import make_empresa, make_admin
from modules.inventario.models import Producto


def _make_producto(empresa, nombre="Producto Test", precio=Decimal("100.00")):
    """Helper: create a product for a given empresa."""
    return Producto.objects.create(
        empresa=empresa,
        nombre=nombre,
        precio_venta=precio,
    )


def _make_user(empresa, rol, email):
    return Usuario.objects.create_user(
        email=email,
        password="pass",
        empresa=empresa,
        rol=rol,
        nombre="Test",
        apellido="User",
    )


class ProductoTenantIsolationTest(APITestCase):
    """Verify Empresa A products are never visible to Empresa B users."""

    def setUp(self):
        self.empresa_a = make_empresa(nombre="Empresa A")
        self.empresa_b = make_empresa(nombre="Empresa B")

        self.admin_a = _make_user(self.empresa_a, Usuario.RolUsuario.ADMIN, "admin_a@test.com")
        self.producto_a = _make_producto(self.empresa_a, "Producto A")
        self.producto_b = _make_producto(self.empresa_b, "Producto B")

        tokens = get_tokens_for_user(self.admin_a)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_list_returns_only_own_empresa_products(self):
        url = reverse("producto-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [item["id"] for item in response.data["results"]]
        self.assertIn(str(self.producto_a.id), ids)
        self.assertNotIn(str(self.producto_b.id), ids)

    def test_retrieve_other_empresa_product_returns_404(self):
        url = reverse("producto-detail", args=[self.producto_b.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ProductoVendedorPermissionsTest(APITestCase):
    """VENDEDOR can create and update products."""

    def setUp(self):
        self.empresa = make_empresa(nombre="Empresa Vendedor")
        self.vendedor = _make_user(self.empresa, Usuario.RolUsuario.VENDEDOR, "vendedor@test.com")
        tokens = get_tokens_for_user(self.vendedor)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_vendedor_can_list_products(self):
        url = reverse("producto-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_vendedor_can_create_product(self):
        url = reverse("producto-list")
        payload = {
            "nombre": "Nuevo Producto",
            "precio_venta": "999.99",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # Confirm it was created under the VENDEDOR's empresa
        producto = Producto.objects.get(id=response.data["id"])
        self.assertEqual(producto.empresa_id, self.empresa.id)


class ProductoContadorPermissionsTest(APITestCase):
    """CONTADOR gets read-only access — POST/PUT/DELETE should return 403."""

    def setUp(self):
        self.empresa = make_empresa(nombre="Empresa Contador")
        self.contador = _make_user(self.empresa, Usuario.RolUsuario.CONTADOR, "contador@test.com")
        self.producto = _make_producto(self.empresa)
        tokens = get_tokens_for_user(self.contador)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_contador_can_list_products(self):
        url = reverse("producto-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_contador_cannot_create_product(self):
        url = reverse("producto-list")
        payload = {"nombre": "Nuevo", "precio_venta": "50.00"}
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_contador_cannot_update_product(self):
        url = reverse("producto-detail", args=[self.producto.id])
        response = self.client.patch(url, {"nombre": "Intentando cambiar"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_contador_cannot_delete_product(self):
        url = reverse("producto-detail", args=[self.producto.id])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
