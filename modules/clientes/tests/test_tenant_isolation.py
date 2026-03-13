from django.test import TestCase
from apps.empresas.models import Empresa
from modules.clientes.models import Cliente
from core.utils.tenant_context import set_current_empresa, reset_current_empresa

class TenantIsolationTest(TestCase):
    def setUp(self):
        self.empresa_a = Empresa.objects.create(nombre="Empresa A", slug="a", plan="free")
        self.empresa_b = Empresa.objects.create(nombre="Empresa B", slug="b", plan="free")
        
        # Create a client for Empresa A
        self.cliente_a = Cliente.objects.create(
            empresa=self.empresa_a,
            nombre="Cliente A",
            email="a@example.com"
        )
        
        # Create a client for Empresa B
        self.cliente_b = Cliente.objects.create(
            empresa=self.empresa_b,
            nombre="Cliente B",
            email="b@example.com"
        )

    def test_isolation_empresa_a(self):
        """When context is Empresa A, only its clients should be visible."""
        token = set_current_empresa(self.empresa_a.id)
        try:
            clientes = Cliente.objects.all()
            self.assertEqual(clientes.count(), 1)
            self.assertEqual(clientes[0].id, self.cliente_a.id)
        finally:
            reset_current_empresa(token)

    def test_isolation_empresa_b(self):
        """When context is Empresa B, only its clients should be visible."""
        token = set_current_empresa(self.empresa_b.id)
        try:
            clientes = Cliente.objects.all()
            self.assertEqual(clientes.count(), 1)
            self.assertEqual(clientes[0].id, self.cliente_b.id)
        finally:
            reset_current_empresa(token)

    def test_no_context_visibility(self):
        """Without context, all active clients should be visible (default behavior)."""
        clientes = Cliente.objects.all()
        # In this implementation, if context is None, no filtering is applied
        self.assertEqual(clientes.count(), 2)

    def test_manager_explicit_filtering(self):
        """Explicit for_empresa should still work even if context is set."""
        token = set_current_empresa(self.empresa_a.id)
        try:
            # Even if we are in Empresa A context, we can explicitly ask for B
            clientes_b = Cliente.objects.for_empresa(self.empresa_b)
            self.assertEqual(clientes_b.count(), 1)
            self.assertEqual(clientes_b[0].id, self.cliente_b.id)
        finally:
            reset_current_empresa(token)
