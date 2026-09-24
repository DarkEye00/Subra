import json
import logging
from hmac import compare_digest

from django.conf import settings
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services import handle_callback

log = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def mpesa_callback(request, secret):
    # Daraja doesn't sign callbacks, so the URL carries a secret only we and Safaricom know.
    if not settings.MPESA_CALLBACK_SECRET or not compare_digest(secret, settings.MPESA_CALLBACK_SECRET):
        raise Http404
    try:
        payload = json.loads(request.body)
        payload["Body"]["stkCallback"]["CheckoutRequestID"]
    except (ValueError, KeyError, TypeError):
        return HttpResponseBadRequest("malformed callback")
    handle_callback(payload)
    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})
