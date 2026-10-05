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
from billing.providers import PaymentEvent, get_provider
from core.ratelimit import allow

from . import services
from .files import read_token, signed_url
from .models import Order, Theme
from .processing import download_name

S = Order.Status
IN_FLIGHT = set(Order.IN_FLIGHT)


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


def _dev_payment_enabled():
    """The simulated-payment button: DEBUG only, and only while the payment provider has no keys."""
    return settings.DEBUG and not get_provider().configured


def _poll_key(order, last_payment):
    """Changes whenever the page should reload: processing status, payment, or the latest payment's status."""
    return f"{order.status}:{int(order.is_paid)}:{last_payment.status if last_payment else '-'}"


def _polling(order, last_payment):
    return order.status in IN_FLIGHT or (last_payment is not None and last_payment.status == Payment.Status.PENDING)


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
        "polling": _polling(order, last_payment),
        "poll_key": _poll_key(order, last_payment),
        "blocking": services.unpaid_work(request.user, exclude=order) if order.status == S.QUOTED else None,
        "dev_payment": _dev_payment_enabled() and order.can_pay,
        "retention_days": settings.FILE_RETENTION_DAYS,
        "unpaid_hours": settings.UNPAID_RETENTION_HOURS,
    }
    if order.status in (S.READY, S.RERENDERING) and order.result_file:
        context["previews"] = [signed_url(name) for name in order.preview_images]
        context["overflow"] = order.overflow_slides
    return render(request, "orders/detail.html", context)


@login_required
@require_POST
def polish(request, public_id):
    order = _own_order(request, public_id)
    theme = Theme.objects.filter(slug=request.POST.get("theme"), active=True).first()
    if theme is None:
        messages.error(request, "Choose a theme.")
        return redirect("orders:detail", public_id=public_id)
    try:
        services.start_generation(order, theme=theme)
    except services.GenerationError as e:
        messages.error(request, e.message)
    return redirect("orders:detail", public_id=public_id)


@login_required
@require_POST
def switch_theme(request, public_id):
    order = _own_order(request, public_id)
    theme = Theme.objects.filter(slug=request.POST.get("theme"), active=True).first()
    if theme is None:
        messages.error(request, "Choose a theme.")
    elif not allow(request, "theme", *settings.THEME_SWITCH_RATE_LIMIT):
        messages.error(request, "You've switched themes a lot in the last hour. Please try again later.")
    else:
        try:
            services.start_theme_switch(order, theme)
        except services.ThemeSwitchError as e:
            messages.error(request, e.message)
    return redirect("orders:detail", public_id=public_id)


@login_required
@require_POST
def pay(request, public_id):
    order = _own_order(request, public_id)
    if not allow(request, "pay", *settings.PAYMENT_RATE_LIMIT):
        messages.error(request, "Too many payment attempts. Please wait a few minutes and try again.")
        return redirect("orders:detail", public_id=public_id)
    try:
        billing.start_payment(order, request.POST.get("phone", ""))
    except billing.PaymentError as e:
        messages.error(request, str(e))
    return redirect("orders:detail", public_id=public_id)


@login_required
def status(request, public_id):
    """HTMX poll target: reload the page when the processing status or the payment changes."""
    order = _own_order(request, public_id)
    last_payment = order.payments.first()
    key = _poll_key(order, last_payment)
    if key != request.GET.get("from"):
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    paying = last_payment is not None and last_payment.status == Payment.Status.PENDING
    waited = (timezone.now() - (last_payment.created_at if paying else order.updated_at)).total_seconds()
    return render(request, "orders/_status.html",
                  {"order": order, "last_payment": last_payment, "poll_key": key, "waited": waited})


@login_required
@require_POST
def discard(request, public_id):
    order = _own_order(request, public_id)
    try:
        services.discard_order(order)
    except ValueError:
        messages.error(request, "This deck can't be discarded right now.")
        return redirect("orders:detail", public_id=public_id)
    messages.success(request, f"{order.original_name} was discarded and its files deleted.")
    return redirect("orders:list")


@login_required
def download(request, public_id):
    order = _own_order(request, public_id)
    if order.status != S.READY or not order.result_file or not order.is_paid:
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
    """DEBUG only, while the payment provider has no keys: pretend M-Pesa confirmed the payment."""
    if not _dev_payment_enabled():
        raise Http404
    order = _own_order(request, public_id)
    if not order.can_pay:
        messages.error(request, "This deck can't be paid for right now.")
        return redirect("orders:detail", public_id=public_id)
    ref = f"dev-{order.public_id.hex[:12]}-{order.payments.count()}"
    Payment.objects.create(order=order, provider=settings.PAYMENT_PROVIDER, provider_ref=ref,
                           phone="254700000000", amount_kes=order.price_kes)
    billing.apply_event(settings.PAYMENT_PROVIDER, PaymentEvent(
        provider_ref=ref, state="paid", amount=Decimal(order.price_kes), receipt="DEVPAY", fee=Decimal("0.01"),
        raw={"simulated": True}), source="callback")
    return redirect("orders:detail", public_id=public_id)
