import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import transaction

from billing.models import PriceTier, ProductType
from polisher.extract import extract_deck
from polisher.pipeline import UnsupportedContentError
from polisher.validate import MESSAGES, ValidationError, validate_file

from .models import Order, Theme

S = Order.Status


class UploadError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class RerunError(Exception):
    pass


def default_theme():
    return Theme.objects.filter(active=True).first()


def create_order(user, uploaded_file, product_type=ProductType.DECK_POLISH):
    """Validate an upload and create a QUOTED order. Nothing is charged or sent to AI here."""
    name = Path(uploaded_file.name).name[:255]
    if uploaded_file.size > settings.MAX_UPLOAD_BYTES:
        raise UploadError(
            f"This file is {uploaded_file.size / 1024 / 1024:.1f} MB. The limit is "
            f"{settings.MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        )
    with tempfile.TemporaryDirectory(prefix="subra-upload-") as tmp:
        path = Path(tmp) / ("upload" + Path(name).suffix.lower())
        with open(path, "wb") as out:
            for chunk in uploaded_file.chunks():
                out.write(chunk)
        try:
            info = validate_file(path, max_bytes=settings.MAX_UPLOAD_BYTES, max_slides=settings.MAX_SLIDES)
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
            )
            with open(path, "rb") as f:
                order.original_file.save("original.pptx", File(f), save=True)
            order.transition(S.QUOTED, note=f"{info.slide_count} slides, {tier.name} tier, KES {tier.price_kes}")
    return order


def set_theme(order, theme):
    if order.status not in (S.QUOTED, S.PAYMENT_FAILED):
        raise ValueError("The theme can only be changed before payment")
    order.theme = theme
    order.save(update_fields=["theme", "updated_at"])


def start_rerun(order, theme):
    """The one free re-run per paid order: same deck, different theme."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if not order.can_rerun:
            raise RerunError("This order has already used its free re-run.")
        order.transition(S.PAID, note=f"Free re-run with theme {theme.slug}", theme=theme, rerun_used=True)
        transaction.on_commit(lambda: _enqueue(order))
    return order


def _enqueue(order):
    from .tasks import process_order

    process_order.delay(order.pk)
