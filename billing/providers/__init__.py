"""Payment providers behind one interface (build plan, Amendment 1A).

A provider sends the payment request, authenticates and normalises callbacks,
and checks status. It never touches the database: billing.services applies the
normalised PaymentEvent idempotently, the same way for every provider.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class ProviderError(Exception):
    """The provider couldn't do what we asked (network, bad request, outage)."""


class CallbackRejected(Exception):
    """A callback failed authentication; it must not change anything."""


@dataclass
class InitResult:
    provider_ref: str  # the provider's ID for this payment, used by callbacks and status checks
    reference: str  # our reference sent with the request (IntaSend echoes it back as api_ref)
    raw: dict = field(default_factory=dict)


@dataclass
class PaymentEvent:
    provider_ref: str
    state: str  # "paid" | "failed" | "pending"
    amount: Decimal | None = None
    receipt: str = ""
    fee: Decimal | None = None
    code: str = ""
    reason: str = ""
    reference: str = ""
    raw: dict = field(default_factory=dict)


class PaymentProvider(Protocol):
    name: str

    def initiate(self, order, phone) -> InitResult: ...

    def handle_callback(self, payload) -> PaymentEvent: ...

    def check_status(self, ref) -> PaymentEvent: ...


def get_provider(name=None):
    name = name or settings.PAYMENT_PROVIDER
    if name == "intasend":
        from .intasend import IntaSendProvider

        return IntaSendProvider.from_settings()
    if name == "daraja":
        raise ImproperlyConfigured("Daraja is a future provider and isn't configured for launch")
    raise ImproperlyConfigured(f"Unknown PAYMENT_PROVIDER '{name}'")
