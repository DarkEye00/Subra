import logging

from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.ratelimit import client_ip

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
        handle_callback(provider, request.body, request.headers)
    except CallbackRejected:
        log.warning("Rejected %s webhook from %s", provider, client_ip(request))
        return HttpResponseForbidden("rejected")
    except ValueError:
        return HttpResponseBadRequest("malformed webhook")
    return JsonResponse({"status": "ok"})
