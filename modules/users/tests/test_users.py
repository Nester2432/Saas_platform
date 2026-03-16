import uuid
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status


from apps.usuarios.models import Usuario
from apps.usuarios.auth.serializers import get_tokens_for_user
from modules.ventas.tests.factories import make_empresa


class UserTenantIsolationTest(APITestCase):
    def setUp(self):
        # 1. First Empresa
        self.empresa_a = make_empresa(nombre="Empresa A")
        self.admin_a = Usuario.objects.create_user(
            email="admin_a@test.com", password="pass",
            empresa=self.empresa_a, rol=Usuario.RolUsuario.ADMIN,
            nombre="Admin", apellido="A"
        )
        self.user_a = Usuario.objects.create_user(
            email="user_a@test.com", password="pass",
            empresa=self.empresa_a, rol=Usuario.RolUsuario.VENDEDOR,
            nombre="Vendedor", apellido="A"
        )
        
        # 2. Second Empresa
        self.empresa_b = make_empresa(nombre="Empresa B")
        self.admin_b = Usuario.objects.create_user(
            email="admin_b@test.com", password="pass",
            empresa=self.empresa_b, rol=Usuario.RolUsuario.ADMIN,
            nombre="Admin", apellido="B"
        )
        
        # Base URLs
        self.users_url = reverse("users-list")
        
        # Auth Token
        self.tokens_a = get_tokens_for_user(self.admin_a)

    def test_tenant_isolation_list_users(self):
        """
        ADMIN A can only see users of Empresa A, never Empresa B.
        """
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_a['access']}")
        response = self.client.get(self.users_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [item["id"] for item in response.data["results"]]
        
        # User A and Admin A belong to Empresa A.
        self.assertEqual(len(ids), 2)
        self.assertIn(str(self.admin_a.id), ids)
        self.assertIn(str(self.user_a.id), ids)
        
        # Admin B securely hidden
        self.assertNotIn(str(self.admin_b.id), ids)

    def test_admin_role_enforcement(self):
        """
        VENDEDOR A cannot list users because they lack ADMIN rol.
        """
        tokens_vend = get_tokens_for_user(self.user_a)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens_vend['access']}")
        
        response = self.client.get(self.users_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_auth_context_automatic_empresa_assignment(self):
        """
        When creating a user, the API forces the assignment to the creator's Empresa. 
        Even if another empresa ID is injected.
        """
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_a['access']}")
        
        payload = {
            "email": "new_user@test.com",
            "password": "secure_password",
            "nombre": "New",
            "apellido": "User",
            "rol": "VENDEDOR",
            "empresa_id": str(self.empresa_b.id)  # Malicious spoof attempt
        }
        
        response = self.client.post(self.users_url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        new_user = Usuario.objects.get(email="new_user@test.com")
        # System overriding spoof attempt via context.
        self.assertEqual(new_user.empresa_id, self.empresa_a.id)

    def test_unique_email_per_empresa(self):
        """
        Testing the database uniqueness constraint.
        Since email is actually globally unique in this implementation (due to USERNAME_FIELD limitations),
        attempting to create the same email again should fail.
        """
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_a['access']}")
        
        payload = {
            "email": "admin_a@test.com",  # Already exists
            "password": "secure_password",
            "nombre": "Clone",
            "apellido": "User",
            "rol": "VENDEDOR",
        }
        
        response = self.client.post(self.users_url, payload)
        # Should fail model validation (400) not 500 constraint failure.
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The serializer returns a dict with field errors or non_field_errors wrapped in 'details'
        resp_json = response.data.get("details", {})
        has_email_error = ("email" in resp_json) or ("non_field_errors" in resp_json)
        self.assertTrue(has_email_error, f"Expected email/non_field_errors in details, got: {response.data}")
