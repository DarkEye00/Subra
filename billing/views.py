import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.ratelimit import client_ip

from . import subscriptions
from .models import Payment
from .providers import CallbackRejected
from .services import handle_callback

log = logging.getLogger(__name__)
WEBHOOK_PROVIDERS = {"paystack", "intasend"}  # Daraja doesn't sign callbacks, so it gets no public webhook


@csrf_exempt
@require_POST
def webhook(request, provider):
    if provider not in WEBHOOK_PROVIDERS:
        raise Http404
    try:
        if provider == "paystack":
            subscriptions.handle_paystack_webhook(request.body, request.headers)
        else:
            handle_callback(provider, request.body, request.headers)
    except CallbackRejected:
        log.warning("Rejected %s webhook from %s", provider, client_ip(request))
        return HttpResponseForbidden("rejected")
    except ValueError:
        return HttpResponseBadRequest("malformed webhook")
    return JsonResponse({"status": "ok"})


@login_required
def card_return(request):
    """Paystack sends the customer back here after its checkout page (?reference=...&trxref=...)."""
    reference = request.GET.get("reference") or request.GET.get("trxref") or ""
    payment = subscriptions.card_return(request.user, reference[:64]) if reference else None
    if payment is None:
        messages.error(request, "We couldn't find that payment.")
    elif payment.status == Payment.Status.PAID:
        if payment.kind == Payment.Kind.TOPUP:
            messages.success(request, f"Payment received. {payment.credit_pack.credits} credits added.")
        else:
            messages.success(request, f"Payment received. Your {payment.subscription.plan.name} plan is active.")
    elif payment.status == Payment.Status.PENDING:
        messages.success(request, "We're confirming your payment with Paystack. This page will show it shortly.")
    else:
        messages.error(request, f"{payment.customer_message} You haven't been charged.")
    return redirect("accounts:plan")
