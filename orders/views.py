from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from billing import services as billing
from billing.models import Payment, PriceTier, ProductType
from billing.providers import PaymentEvent
from core.ratelimit import allow

from . import services
from .files import read_token, signed_url
from .models import Order, Theme
from .processing import download_name

S = Order.Status
POLLING = {S.PAYMENT_PENDING, S.PAID, S.PROCESSING}


def home(request):
    context = {
        "tiers": PriceTier.objects.filter(active=True, product_type=ProductType.DECK_POLISH),
        "themes": Theme.objects.filter(active=True),
        "max_mb": settings.MAX_UPLOAD_BYTES // 1024 // 1024,
        "max_media_mb": settings.MAX_UPLOAD_BYTES_MEDIA // 1024 // 1024,
        "max_slides": settings.MAX_SLIDES,
        "retention_days": settings.FILE_RETENTION_DAYS,
    }
    if request.user.is_authenticated:
        context["recent"] = request.user.orders.all()[:3]
    return render(request, "orders/home.html", context)


@login_required
@require_POST
def upload(request):
    if not allow(request, "upload", *settings.UPLOAD_RATE_LIMIT):
        messages.error(request, "You've uploaded a lot of decks in the last hour. Please try again later.")
        return redirect("home")
    deck = request.FILES.get("deck")
    if deck is None:
        messages.error(request, "Choose a .pptx file to upload.")
        return redirect("home")
    try:
        order = services.create_order(request.user, deck)
    except services.UploadError as e:
        messages.error(request, e.message)
        return redirect("home")
    return redirect("orders:detail", public_id=order.public_id)


def _own_order(request, public_id):
    return get_object_or_404(Order, public_id=public_id, user=request.user)


def _local_phone(mpesa_phone):
    return "0" + mpesa_phone[3:] if mpesa_phone.startswith("254") and len(mpesa_phone) == 12 else ""


@login_required
def order_list(request):
    return render(request, "orders/list.html", {"orders": request.user.orders.select_related("theme")})


@login_required
def detail(request, public_id):
    order = _own_order(request, public_id)
    last_payment = order.payments.first()
    context = {
        "order": order,
        "themes": Theme.objects.filter(active=True),
        "phone": _local_phone(last_payment.phone if last_payment else request.user.mpesa_phone),
        "last_payment": last_payment,
        "polling": order.status in POLLING,
        "dev_payment": settings.DEBUG and not settings.INTASEND_SECRET_KEY,
        "retention_days": settings.FILE_RETENTION_DAYS,
    }
    if order.status == S.DONE and order.result_file:
        context["previews"] = [signed_url(name) for name in order.preview_images]
        context["overflow"] = order.overflow_slides
    return render(request, "orders/detail.html", context)


@login_required
@require_POST
def pay(request, public_id):
    order = _own_order(request, public_id)
    if not allow(request, "pay", *settings.PAYMENT_RATE_LIMIT):
        messages.error(request, "Too many payment attempts. Please wait a few minutes and try again.")
        return redirect("orders:detail", public_id=public_id)
    theme = Theme.objects.filter(slug=request.POST.get("theme"), active=True).first()
    if theme is None:
        messages.error(request, "Choose a theme.")
        return redirect("orders:detail", public_id=public_id)
    try:
        services.set_theme(order, theme)
        billing.start_payment(order, request.POST.get("phone", ""))
    except billing.PaymentError as e:
        messages.error(request, str(e))
    except ValueError:
        messages.error(request, "This order can't be paid right now.")
    return redirect("orders:detail", public_id=public_id)


@login_required
def status(request, public_id):
    """HTMX poll target: reload the page when the status changes."""
    order = _own_order(request, public_id)
    if order.status != request.GET.get("from"):
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    waited = (timezone.now() - order.updated_at).total_seconds()
    return render(request, "orders/_status.html", {"order": order, "waited": waited})


@login_required
@require_POST
def rerun(request, public_id):
    order = _own_order(request, public_id)
    theme = Theme.objects.filter(slug=request.POST.get("theme"), active=True).first()
    if theme is None or theme == order.theme:
        messages.error(request, "Choose a different theme for your free re-run.")
        return redirect("orders:detail", public_id=public_id)
    try:
        services.start_rerun(order, theme)
    except services.RerunError as e:
        messages.error(request, str(e))
    return redirect("orders:detail", public_id=public_id)


@login_required
def download(request, public_id):
    order = _own_order(request, public_id)
    if order.status != S.DONE or not order.result_file:
        raise Http404
    return redirect(signed_url(order.result_file.name, download_name(order)))


def serve_file(request, token):
    """Local-storage stand-in for signed Spaces URLs (development only)."""
    try:
        data = read_token(token)
    except signing.BadSignature:
        raise Http404
    if not default_storage.exists(data["n"]):
        raise Http404
    return FileResponse(default_storage.open(data["n"], "rb"), as_attachment=bool(data["d"]), filename=data["d"])


@login_required
@require_POST
def dev_pay(request, public_id):
    """DEBUG only, when no IntaSend keys are set: pretend M-Pesa confirmed the payment."""
    if not settings.DEBUG or settings.INTASEND_SECRET_KEY:
        raise Http404
    order = _own_order(request, public_id)
    theme = Theme.objects.filter(slug=request.POST.get("theme"), active=True).first()
    if theme:
        services.set_theme(order, theme)
    ref = f"dev-{order.public_id.hex[:12]}-{order.payments.count()}"
    Payment.objects.create(order=order, provider=settings.PAYMENT_PROVIDER, provider_ref=ref,
                           phone="254700000000", amount_kes=order.price_kes)
    order.transition(S.PAYMENT_PENDING, note="Simulated payment (development)")
    billing.apply_event(settings.PAYMENT_PROVIDER, PaymentEvent(
        provider_ref=ref, state="paid", amount=Decimal(order.price_kes), receipt="DEVPAY", fee=Decimal("0.01"),
        raw={"simulated": True}), source="callback")
    return redirect("orders:detail", public_id=public_id)
