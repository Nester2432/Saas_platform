from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import EventStoreViewSet
from .demo_views import DemoFullFlowView, DemoResourcesView, DemoStatusView, DemoActionView

router = DefaultRouter()
router.register(r"event-store", EventStoreViewSet, basename="event-store")

urlpatterns = [
    path("", include(router.urls)),
    path("demo/full-flow/", DemoFullFlowView.as_view(), name="demo-full-flow"),
    path("demo/resources/", DemoResourcesView.as_view(), name="demo-resources"),
    path("demo/status/", DemoStatusView.as_view(), name="demo-status"),
    path("demo/action/", DemoActionView.as_view(), name="demo-action"),
]
