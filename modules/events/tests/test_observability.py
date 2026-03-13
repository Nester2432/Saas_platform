import pytest
from rest_framework import status
from django.urls import reverse
from rest_framework.test import APIClient
from modules.events.models import EventStore, EventStatus
from modules.events import events

@pytest.mark.django_db
class TestObservability:
    @pytest.fixture
    def api_client(self):
        return APIClient()

    def test_demo_status_view(self, api_client, admin_user_fixture, empresa_fixture):
        admin_user_fixture.is_staff = True
        admin_user_fixture.is_superuser = True
        admin_user_fixture.save()
        api_client.force_authenticate(user=admin_user_fixture)
        
        # Create some events
        EventStore.objects.create(
            event_name=events.VENTA_CREADA,
            event_id="00000000-0000-0000-0000-000000000001",
            empresa_id=str(empresa_fixture.id),
            payload={"test": "data"},
            status=EventStatus.PROCESSED
        )
        
        url = reverse("demo-status")
        response = api_client.get(url)
        
        assert response.status_code == status.HTTP_200_OK
        assert "metrics" in response.data
        assert response.data["metrics"]["status_breakdown"]["PROCESSED"] >= 1

    def test_demo_resources_view(self, api_client, admin_user_fixture, empresa_fixture):
        admin_user_fixture.is_staff = True
        admin_user_fixture.is_superuser = True
        admin_user_fixture.save()
        api_client.force_authenticate(user=admin_user_fixture)
        
        url = reverse("demo-resources")
        response = api_client.get(url)
        
        assert response.status_code == status.HTTP_200_OK
        assert "recent_resources" in response.data

    def test_event_store_filtering(self, api_client, admin_user_fixture, empresa_fixture):
        admin_user_fixture.is_staff = True
        admin_user_fixture.is_superuser = True
        admin_user_fixture.save()
        api_client.force_authenticate(user=admin_user_fixture)
        
        # Create events with different names
        EventStore.objects.create(
            event_name="event_a",
            event_id="00000000-0000-0000-0000-000000000002",
            empresa_id=str(empresa_fixture.id),
            payload={},
            status=EventStatus.PENDING
        )
        EventStore.objects.create(
            event_name="event_b",
            event_id="00000000-0000-0000-0000-000000000003",
            empresa_id=str(empresa_fixture.id),
            payload={},
            status=EventStatus.PROCESSED
        )
        
        url = reverse("event-store-list")
        
        # Filter by name
        response = api_client.get(f"{url}?event_name=event_a")
        assert response.status_code == status.HTTP_200_OK
        results = response.data.get("results", response.data)
        assert len(results) == 1
        assert results[0]["event_name"] == "event_a"
        
        # Filter by status
        response = api_client.get(f"{url}?status=PROCESSED")
        results = response.data.get("results", response.data)
        # May have other processed events from elsewhere
        assert len(results) >= 1
        assert any(e["event_name"] == "event_b" for e in results)
