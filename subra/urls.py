from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts.forms import EmailAuthenticationForm
from accounts.views import signup
from core import views as core_views
from orders.views import home

admin.site.site_header = "SUBRA admin"
admin.site.site_title = "SUBRA admin"

urlpatterns = [
    path("", home, name="home"),
    path("orders/", include("orders.urls")),
    path("billing/", include("billing.urls")),
    path("accounts/signup/", signup, name="signup"),
    path("accounts/", include("accounts.urls")),
    path("accounts/login/", auth_views.LoginView.as_view(
        template_name="accounts/login.html", authentication_form=EmailAuthenticationForm), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("privacy/", core_views.privacy, name="privacy"),
    path("terms/", core_views.terms, name="terms"),
    path("admin/", admin.site.urls),
]
