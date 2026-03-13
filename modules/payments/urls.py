from django.urls import path, include

urlpatterns = [
    path("", include("modules.payments.api.urls")),
]
