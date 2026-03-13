from django.urls import path
from .demo_views import DemoDashboardView

urlpatterns = [
    path("demo/dashboard/", DemoDashboardView.as_view(), name="demo-dashboard"),
]
