import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts.models import User
from billing.models import Payment
from deckbuilder import build_rich_deck
from orders import services, tasks
from orders.models import Order

S = Order.Status


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def deck_bytes(tmp_path):
    return build_rich_deck(tmp_path / "rich.pptx").read_bytes()


def pptx(data, name="Board pack.pptx"):
    return SimpleUploadedFile(name, data, content_type="application/octet-stream")


def page(client, order):
    return client.get(reverse("orders:detail", args=[order.public_id])).content.decode()


# --- home and upload --------------------------------------------------------

def test_home_for_visitors_shows_prices_and_signup(client, db):
    html = client.get("/").content.decode()
    assert "Upload a rough deck" in html and "KES 350" in html and reverse("signup") in html
    assert 'name="deck"' not in html


def test_home_for_customers_shows_the_upload_form(logged_in):
    assert 'name="deck"' in logged_in.get("/").content.decode()


def test_upload_requires_login(client, db, deck_bytes):
    resp = client.post(reverse("orders:upload"), {"deck": pptx(deck_bytes)})
    assert resp.status_code == 302 and reverse("login") in resp["Location"]


def test_upload_leads_to_a_quote(logged_in, deck_bytes):
    html = logged_in.post(reverse("orders:upload"), {"deck": pptx(deck_bytes)}, follow=True).content.decode()
    assert "Board pack.pptx" in html and "KES 200" in html and "Polish my deck" in html
    assert "You pay only after you see your deck" in html


def test_bad_upload_explains_the_problem(logged_in):
    resp = logged_in.post(reverse("orders:upload"), {"deck": pptx(b"%PDF", "notes.pdf")}, follow=True)
    assert "Please upload a PowerPoint file saved as .pptx." in resp.content.decode()
    assert not Order.objects.exists()


def test_uploads_are_rate_limited(logged_in, deck_bytes, settings):
    settings.UPLOAD_RATE_LIMIT = (1, 3600)
    logged_in.post(reverse("orders:upload"), {"deck": pptx(deck_bytes)})
    resp = logged_in.post(reverse("orders:upload"), {"deck": pptx(deck_bytes)}, follow=True)
    assert "try again later" in resp.content.decode()
    assert Order.objects.count() == 1


def test_other_customers_orders_are_hidden(client, quoted_order):
    client.force_login(User.objects.create_user("other@example.com", "another-password-9"))
    assert client.get(reverse("orders:detail", args=[quoted_order.public_id])).status_code == 404


# --- polishing --------------------------------------------------------------

def test_polish_queues_the_deck_in_the_chosen_theme(logged_in, quoted_order):
    logged_in.post(reverse("orders:polish", args=[quoted_order.public_id]), {"theme": "slate"})
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.QUEUED and quoted_order.theme.slug == "slate"


def test_polish_needs_a_theme(logged_in, quoted_order):
    resp = logged_in.post(reverse("orders:polish", args=[quoted_order.public_id]), {}, follow=True)
    assert "Choose a theme." in resp.content.decode()


def test_one_unpaid_deck_at_a_time(logged_in, order_factory):
    first = order_factory("First.pptx", status=S.READY)
    second = order_factory("Second.pptx")
    html = page(logged_in, second)
    assert "Pay for or discard" in html and reverse("orders:detail", args=[first.public_id]) in html
    resp = logged_in.post(reverse("orders:polish", args=[second.public_id]), {"theme": "navy"}, follow=True)
    assert "Pay for or discard First.pptx first." in resp.content.decode()
    second.refresh_from_db()
    assert second.status == S.QUOTED


# --- paying and downloading ---------------------------------------------------

def test_a_ready_deck_shows_previews_and_a_pay_button_but_no_download(logged_in, ready_order):
    html = page(logged_in, ready_order)
    assert "Pay KES 200 to download" in html and "Unpaid decks are deleted after 48 hours" in html
    assert html.count('alt="Slide ') == 2
    assert "Discard this deck" in html
    assert reverse("orders:download", args=[ready_order.public_id]) not in html


def test_a_deck_without_previews_says_so(logged_in, user, tmp_path, monkeypatch):
    from orders import processing
    from polisher.preview import PreviewError

    def broken(*args, **kwargs):
        raise PreviewError("LibreOffice crashed")

    monkeypatch.setattr(processing, "render_previews", broken)
    order = services.create_order(user, pptx(build_rich_deck(tmp_path / "d.pptx").read_bytes()))
    services.start_generation(order)
    tasks.process_order(order.pk)
    html = page(logged_in, order)
    assert "We couldn't make preview images for this deck" in html
    assert "Pay KES 200 to download" in html


def test_pay_starts_mpesa(logged_in, ready_order, monkeypatch):
    from billing import services as billing

    calls = []
    monkeypatch.setattr(billing, "start_payment", lambda order, phone: calls.append((order.pk, phone)))
    logged_in.post(reverse("orders:pay", args=[ready_order.public_id]), {"phone": "0712345678"})
    assert calls == [(ready_order.pk, "0712345678")]


def test_pay_shows_phone_errors(logged_in, order_factory):
    order = order_factory(status=S.READY)
    resp = logged_in.post(reverse("orders:pay", args=[order.public_id]), {"phone": "123"}, follow=True)
    assert "Enter a phone number like 0712 345 678" in resp.content.decode()


def test_a_pending_payment_shows_check_your_phone(logged_in, order_factory):
    order = order_factory(status=S.READY)
    Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=350)
    html = page(logged_in, order)
    assert "Check your phone" in html and "Pay KES 350 to download" not in html


def test_a_failed_payment_explains_and_offers_a_retry(logged_in, order_factory):
    order = order_factory(status=S.READY)
    Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=350, status="failed", result_code="1032")
    html = page(logged_in, order)
    assert "You cancelled the M-Pesa prompt." in html and "Pay KES 350 to download" in html


def test_status_poll_reloads_when_either_track_changes(logged_in, order_factory):
    order = order_factory(status=S.READY)
    url = reverse("orders:status", args=[order.public_id])
    assert logged_in.get(url, {"from": "ready:0:-"}).status_code == 200
    Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=350)
    changed = logged_in.get(url, {"from": "ready:0:-"})
    assert changed.status_code == 204 and changed["HX-Refresh"] == "true"
    assert logged_in.get(url, {"from": "ready:0:pending"}).status_code == 200


def test_download_is_locked_until_paid(logged_in, ready_order):
    assert logged_in.get(reverse("orders:download", args=[ready_order.public_id])).status_code == 404


def test_a_paid_deck_downloads_with_a_friendly_name(logged_in, paid_order):
    assert "Download the .pptx" in page(logged_in, paid_order)
    resp = logged_in.get(reverse("orders:download", args=[paid_order.public_id]))
    assert resp.status_code == 302
    file_resp = logged_in.get(resp["Location"])
    assert file_resp.status_code == 200
    assert 'filename="Q3 review (polished).pptx"' in file_resp["Content-Disposition"]


def test_tampered_file_links_are_rejected(logged_in, paid_order):
    resp = logged_in.get(reverse("orders:download", args=[paid_order.public_id]))
    assert logged_in.get(resp["Location"][:-3] + "abc/").status_code == 404


def test_discard_deletes_the_deck_and_returns_to_the_list(logged_in, ready_order):
    resp = logged_in.post(reverse("orders:discard", args=[ready_order.public_id]), follow=True)
    assert "was discarded and its files deleted" in resp.content.decode()
    ready_order.refresh_from_db()
    assert ready_order.status == S.EXPIRED


def test_a_failed_deck_says_nothing_was_charged(logged_in, order_factory):
    html = page(logged_in, order_factory(status=S.FAILED))
    assert "We couldn't polish this deck" in html and "You haven't been charged." in html


def test_order_history_shows_paid_and_unpaid(logged_in, paid_order, order_factory):
    order_factory("Waiting.pptx", status=S.READY)
    html = logged_in.get(reverse("orders:list")).content.decode()
    assert ">Paid<" in html and ">Unpaid<" in html


# --- odds and ends ----------------------------------------------------------

def test_simulated_payment_is_unavailable_outside_debug(logged_in, order_factory):
    order = order_factory(status=S.READY)
    assert logged_in.post(reverse("orders:dev_pay", args=[order.public_id])).status_code == 404


def test_simulated_payment_needs_debug_and_no_provider_keys(settings):
    from orders.views import _dev_payment_enabled

    settings.DEBUG, settings.PAYMENT_PROVIDER, settings.INTASEND_SECRET_KEY = True, "intasend", ""
    assert _dev_payment_enabled()
    settings.INTASEND_SECRET_KEY = "ISSecretKey_test"
    assert not _dev_payment_enabled()
    settings.DEBUG, settings.INTASEND_SECRET_KEY = False, ""
    assert not _dev_payment_enabled()


def test_legal_pages_show_a_draft_banner_until_reviewed(client, db, settings):
    assert "Draft" in client.get(reverse("privacy")).content.decode()
    settings.LEGAL_PAGES_REVIEWED = True
    assert "Draft" not in client.get(reverse("terms")).content.decode()


def test_signup_logs_in(client, db):
    resp = client.post(reverse("signup"), {"email": "New@Example.com", "password1": "kahawa-tamu-2026",
                                           "password2": "kahawa-tamu-2026"})
    assert resp.status_code == 302
    assert User.objects.get().email == "new@example.com"


def test_quote_lists_locked_objects_and_warns_about_links(logged_in, tmp_path):
    from lockedbuilder import build_locked_deck

    data = build_locked_deck(tmp_path / "locked.pptx").read_bytes()
    html = logged_in.post(reverse("orders:upload"), {"deck": pptx(data)}, follow=True).content.decode()
    assert "These are kept exactly as they are, and the slides are restyled around them." in html
    assert "links to external files that may not open on other computers" in html


def test_preview_page_notes_equation_rendering(logged_in, user, tmp_path, fake_previews):
    from lockedbuilder import build_locked_deck

    order = services.create_order(user, pptx(build_locked_deck(tmp_path / "locked.pptx").read_bytes()))
    services.start_generation(order)
    tasks.process_order(order.pk)
    order.refresh_from_db()
    assert order.status == S.READY, order.failure_reason
    html = page(logged_in, order)
    assert "equations" in html and "doesn't affect your download" in html
