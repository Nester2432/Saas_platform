from django.urls import path, include
from rest_framework.routers import DefaultRouter
from modules.users.views import AuthViewSet, UserViewSet

router = DefaultRouter()
router.register(r"users", UserViewSet, basename="users")

urlpatterns = [
    # Auth nested under custom AuthViewSet mapped paths
    path("auth/login/", AuthViewSet.as_view({"post": "login"}), name="auth_login_override"),
    path("auth/logout/", AuthViewSet.as_view({"post": "logout"}), name="auth_logout"),
    path("auth/me/", AuthViewSet.as_view({"get": "me"}), name="auth_me"),
    
    # Standard CRUD users underneath
    path("", include(router.urls)),
]
