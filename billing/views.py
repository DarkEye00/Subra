import json
import logging

from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .providers import CallbackRejected
from .services import handle_callback

log = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def intasend_webhook(request):
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict) or "invoice_id" not in payload:
            raise ValueError
    except ValueError:
        return HttpResponseBadRequest("malformed webhook")
    try:
        handle_callback("intasend", payload)
    except CallbackRejected:
        log.warning("Rejected IntaSend webhook for invoice %s", payload.get("invoice_id"))
        return HttpResponseForbidden("challenge mismatch")
    return JsonResponse({"status": "ok"})
