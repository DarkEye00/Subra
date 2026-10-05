from datetime import timedelta

import pytest
from django.core.files.storage import default_storage
from django.utils import timezone

from accounts.models import User
from billing.models import Payment
from orders import services
from orders.models import InvalidTransition, Order, Theme

S = Order.Status

ALLOWED = {
    (S.QUOTED, S.QUEUED), (S.QUOTED, S.EXPIRED),
    (S.QUEUED, S.PROCESSING),
    (S.PROCESSING, S.READY), (S.PROCESSING, S.FAILED), (S.PROCESSING, S.QUEUED),
    (S.READY, S.RERENDERING), (S.READY, S.EXPIRED),
    (S.RERENDERING, S.READY),
    (S.FAILED, S.EXPIRED),
}


@pytest.fixture
def no_enqueue(monkeypatch):
    queued = []
    monkeypatch.setattr(services, "_enqueue", lambda order: queued.append(order.pk))
    return queued


@pytest.mark.parametrize("start", list(S))
@pytest.mark.parametrize("end", list(S))
def test_only_the_designed_transitions_are_allowed(order_factory, start, end):
    order = order_factory(status=start)
    if (start, end) in ALLOWED:
        order.transition(end)
        assert order.status == end
    else:
        with pytest.raises(InvalidTransition):
            order.transition(end)


def test_a_new_order_is_quoted_unpaid_and_kept_48_hours(quoted_order):
    assert quoted_order.status == S.QUOTED and not quoted_order.is_paid
    assert timedelta(hours=47) < quoted_order.expires_at - timezone.now() <= timedelta(hours=48)


def test_generation_queues_the_deck_in_the_chosen_theme(quoted_order, no_enqueue,
                                                         django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        services.start_generation(quoted_order, theme=Theme.objects.get(slug="slate"))
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.QUEUED and quoted_order.theme.slug == "slate"
    assert no_enqueue == [quoted_order.pk]


def test_generation_starts_once(quoted_order, no_enqueue):
    services.start_generation(quoted_order)
    with pytest.raises(services.GenerationError):
        services.start_generation(quoted_order)


@pytest.mark.parametrize("status", [S.QUEUED, S.PROCESSING, S.READY, S.RERENDERING])
def test_an_unpaid_deck_blocks_a_second_one(order_factory, no_enqueue, status):
    first = order_factory("First.pptx", status=status)
    second = order_factory("Second.pptx")
    with pytest.raises(services.GenerationError) as exc:
        services.start_generation(second)
    assert exc.value.message == "Pay for or discard First.pptx first."
    assert exc.value.blocking_order == first
    second.refresh_from_db()
    assert second.status == S.QUOTED


def test_paying_for_the_first_deck_unblocks_the_next(order_factory, no_enqueue):
    order_factory("First.pptx", status=S.READY, paid_at=timezone.now())
    services.start_generation(order_factory("Second.pptx"))


@pytest.mark.parametrize("status", [S.QUOTED, S.FAILED, S.EXPIRED])
def test_decks_without_unpaid_ai_work_dont_block(order_factory, no_enqueue, status):
    order_factory("Other.pptx", status=status)
    services.start_generation(order_factory("New.pptx"))


def test_another_customers_unpaid_deck_doesnt_block(order_factory, no_enqueue):
    other = User.objects.create_user("other@example.com", "another-password-9")
    order_factory("Theirs.pptx", status=S.READY, owner=other)
    services.start_generation(order_factory("Mine.pptx"))


def test_discarding_deletes_the_files_at_once(ready_order):
    names = [ready_order.original_file.name, ready_order.result_file.name, *ready_order.preview_images]
    assert all(default_storage.exists(n) for n in names)
    services.discard_order(ready_order)
    ready_order.refresh_from_db()
    assert ready_order.status == S.EXPIRED and ready_order.files_deleted_at
    assert not any(default_storage.exists(n) for n in names)
    assert ready_order.events.last().note == "Discarded by customer"


@pytest.mark.parametrize("status", [S.QUOTED, S.FAILED])
def test_quoted_and_failed_decks_can_be_discarded(order_factory, status):
    assert services.discard_order(order_factory(status=status)).status == S.EXPIRED


@pytest.mark.parametrize("status", [S.QUEUED, S.PROCESSING, S.RERENDERING, S.EXPIRED])
def test_decks_in_flight_or_gone_cant_be_discarded(order_factory, status):
    with pytest.raises(ValueError):
        services.discard_order(order_factory(status=status))


def test_paid_decks_cant_be_discarded(order_factory):
    with pytest.raises(ValueError):
        services.discard_order(order_factory(status=S.READY, paid_at=timezone.now()))


def test_a_deck_with_a_pending_payment_cant_be_discarded(order_factory):
    order = order_factory(status=S.READY)
    Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=350)
    with pytest.raises(ValueError):
        services.discard_order(order)


def test_a_deck_whose_files_are_gone_never_blocks_a_new_one(order_factory, no_enqueue):
    order_factory("Gone.pptx", status=S.READY, files_deleted_at=timezone.now())
    services.start_generation(order_factory("New.pptx"))
