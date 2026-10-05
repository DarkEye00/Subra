import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "tests"))  # deckbuilder, lockedbuilder


@pytest.fixture(autouse=True)
def _clear_cache():
    """Rate-limit counters live in the cache; don't let them leak between tests."""
    from django.core.cache import cache

    cache.clear()
    yield


@pytest.fixture
def user(db):
    from accounts.models import User

    return User.objects.create_user("wanjiku@example.co.ke", "a-strong-password-1")


@pytest.fixture
def theme(db):
    from orders.models import Theme

    return Theme.objects.get(slug="navy")


@pytest.fixture
def order_factory(db, user, theme):
    """Orders without files (KES 350, 20 slides), for status and payment rules. status= starts elsewhere."""
    from billing.models import PriceTier
    from orders.models import Order

    tier = PriceTier.objects.get(name="Standard")

    def make(name="Q3 review.pptx", status=Order.Status.QUOTED, owner=None, **fields):
        fields.setdefault("price_kes", tier.price_kes)
        order = Order.objects.create(user=owner or user, original_name=name, slide_count=20, tier=tier, theme=theme,
                                     **fields)
        if status != Order.Status.QUOTED:
            Order.objects.filter(pk=order.pk).update(status=status)
            order.refresh_from_db()
        return order

    return make


@pytest.fixture
def quoted_order(order_factory):
    return order_factory()


@pytest.fixture
def fake_previews(monkeypatch):
    """LibreOffice is slow; fake two thumbnails per render."""
    from orders import processing

    def fake(pptx, out_dir, dpi=60):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in (1, 2):
            p = out_dir / f"slide-{i}.png"
            p.write_bytes(b"\x89PNG fake")
            paths.append(p)
        return paths

    monkeypatch.setattr(processing, "render_previews", fake)


def _deck_upload(path, name="Q3 review.pptx"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, Path(path).read_bytes(),
                              content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@pytest.fixture
def ready_order(user, tmp_path, fake_previews):
    """A rich 7-slide deck (KES 200), generated with the heuristic planner and not yet paid for."""
    from deckbuilder import build_rich_deck
    from orders import services, tasks
    from orders.models import Order

    order = services.create_order(user, _deck_upload(build_rich_deck(tmp_path / "rich.pptx")))
    services.start_generation(order)
    tasks.process_order(order.pk)
    order.refresh_from_db()
    assert order.status == Order.Status.READY, order.failure_reason
    return order


def _confirm_payment(order, ref="INV-1", fee="3.00"):
    """Record a confirmed M-Pesa payment for the order, the way a provider webhook would."""
    from billing import services
    from billing.models import Payment
    from billing.providers import PaymentEvent

    Payment.objects.create(order=order, provider="intasend", provider_ref=ref, phone="254712345678",
                           amount_kes=order.price_kes)
    services.apply_event("intasend", PaymentEvent(provider_ref=ref, state="paid", amount=Decimal(order.price_kes),
                                                  receipt="SJK1ABC2DE", fee=Decimal(fee), raw={}), source="callback")
    order.refresh_from_db()
    return order


@pytest.fixture
def confirm_payment(db):
    return _confirm_payment


@pytest.fixture
def paid_order(ready_order):
    return _confirm_payment(ready_order)
