from django.urls import path

from . import views

app_name = "billing"
urlpatterns = [
    path("webhooks/<slug:provider>/", views.webhook, name="webhook"),
    path("card/return/", views.card_return, name="card_return"),
]
