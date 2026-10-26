from django.urls import path

from . import views

app_name = "orders"
urlpatterns = [
    path("upload/", views.upload, name="upload"),
    path("", views.order_list, name="list"),
    path("<uuid:public_id>/", views.detail, name="detail"),
    path("<uuid:public_id>/polish/", views.polish, name="polish"),
    path("<uuid:public_id>/theme/", views.switch_theme, name="theme"),
    path("<uuid:public_id>/pay/", views.pay, name="pay"),
    path("<uuid:public_id>/pay-credits/", views.pay_credits, name="pay_credits"),
    path("<uuid:public_id>/status/", views.status, name="status"),
    path("<uuid:public_id>/discard/", views.discard, name="discard"),
    path("<uuid:public_id>/download/", views.download, name="download"),
    path("<uuid:public_id>/dev-pay/", views.dev_pay, name="dev_pay"),
    path("files/<str:token>/", views.serve_file, name="file"),
]
