import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files import File
from django.db import transaction

from billing.models import PriceTier, ProductType
from polisher.extract import extract_deck
from polisher.pipeline import UnsupportedContentError
from polisher.validate import MESSAGES, ValidationError, validate_file

from . import processing
from .models import Order, OrderEvent, Theme

S = Order.Status


class UploadError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class GenerationError(Exception):
    """Why a deck can't be sent for polishing, in words the customer sees."""

    def __init__(self, message, blocking_order=None):
        super().__init__(message)
        self.message = message
        self.blocking_order = blocking_order


def default_theme():
    return Theme.objects.filter(active=True).first()


def create_order(user, uploaded_file, product_type=ProductType.DECK_POLISH):
    """Validate an upload and create a QUOTED order. Nothing is charged or sent to AI here."""
    name = Path(uploaded_file.name).name[:255]
    hard_limit = max(settings.MAX_UPLOAD_BYTES, settings.MAX_UPLOAD_BYTES_MEDIA)
    if uploaded_file.size > hard_limit:
        raise UploadError(
            f"This file is {uploaded_file.size / 1024 / 1024:.1f} MB. The limit is "
            f"{settings.MAX_UPLOAD_BYTES // 1024 // 1024} MB, or {hard_limit // 1024 // 1024} MB for decks "
            "with video or audio."
        )
    with tempfile.TemporaryDirectory(prefix="subra-upload-") as tmp:
        path = Path(tmp) / ("upload" + Path(name).suffix.lower())
        with open(path, "wb") as out:
            for chunk in uploaded_file.chunks():
                out.write(chunk)
        try:
            info = validate_file(path, max_bytes=settings.MAX_UPLOAD_BYTES, max_slides=settings.MAX_SLIDES,
                                 max_bytes_media=settings.MAX_UPLOAD_BYTES_MEDIA)
            deck = extract_deck(path)
        except ValidationError as e:
            raise UploadError(e.message)
        except Exception:
            raise UploadError(MESSAGES["corrupt"])
        if deck.unsupported:
            raise UploadError(str(UnsupportedContentError(deck.unsupported)))
        tier = PriceTier.for_units(product_type, info.slide_count)
        if tier is None:
            raise UploadError("We don't have a price for a deck of this size yet.")
        with transaction.atomic():
            order = Order.objects.create(
                user=user, product_type=product_type, original_name=name, slide_count=info.slide_count,
                tier=tier, price_kes=tier.price_kes, theme=default_theme(),
                locked_summary=deck.locked_summary(),
            )
            with open(path, "rb") as f:
                order.original_file.save("original.pptx", File(f), save=True)
            OrderEvent.objects.create(order=order, to_status=S.QUOTED,
                                      note=f"{info.slide_count} slides, {tier.name} tier, KES {tier.price_kes}")
    return order


def set_theme(order, theme):
    """Before polishing only. Afterwards a theme change is a re-render."""
    if order.status != S.QUOTED:
        raise ValueError("The theme can only be set here before polishing")
    order.theme = theme
    order.save(update_fields=["theme", "updated_at"])


def unpaid_work(user, exclude=None):
    """The customer's deck that has used AI but isn't paid for yet, if any (one at a time)."""
    orders = Order.objects.filter(user=user, status__in=Order.UNPAID_WORK, paid_at__isnull=True)
    if exclude is not None:
        orders = orders.exclude(pk=exclude.pk)
    return orders.order_by("created_at").first()


def start_generation(order, theme=None):
    """Queue a quoted deck for polishing. Refused while the customer has another unpaid deck."""
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=order.user_id)  # one check at a time per customer
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status != S.QUOTED:
            raise GenerationError("This deck has already been sent for polishing.")
        blocking = unpaid_work(order.user, exclude=order)
        if blocking is not None:
            raise GenerationError(f"Pay for or discard {blocking.original_name} first.", blocking_order=blocking)
        theme = theme or order.theme
        order.transition(S.QUEUED, note=f"Polishing in {theme.slug}", theme=theme)
        transaction.on_commit(lambda: _enqueue(order))
    return order


def discard_order(order):
    """Delete an unpaid deck's files now. Raises ValueError if it can't be discarded."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if not order.can_discard:
            raise ValueError("This deck can't be discarded")
        processing.delete_order_files(order)
        order.transition(S.EXPIRED, note="Discarded by customer")
    return order


def _enqueue(order):
    from .tasks import process_order

    process_order.delay(order.pk)
