from django.urls import path, include

urlpatterns = [
    path("", include("modules.billing.api.urls")),
]
