import sys
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from deckbuilder import build_rich_deck  # noqa: E402

from accounts.models import User  # noqa: E402
from orders import services, tasks  # noqa: E402
from orders.models import Order  # noqa: E402

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


@pytest.fixture
def done_order(user, deck_bytes, monkeypatch):
    from orders import processing

    monkeypatch.setattr(processing, "render_previews", lambda *a, **k: [])
    order = services.create_order(user, pptx(deck_bytes))
    order.transition(S.PAYMENT_PENDING)
    order.transition(S.PAID)
    tasks.process_order(order.pk)
    order.refresh_from_db()
    assert order.status == S.DONE
    return order


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
    resp = logged_in.post(reverse("orders:upload"), {"deck": pptx(deck_bytes)}, follow=True)
    html = resp.content.decode()
    assert "Board pack.pptx" in html and "KES 200" in html and "Pay KES 200 with M-Pesa" in html


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
    other = User.objects.create_user("other@example.com", "another-password-9")
    client.force_login(other)
    assert client.get(reverse("orders:detail", args=[quoted_order.public_id])).status_code == 404


def test_pay_starts_mpesa_with_the_chosen_theme(logged_in, quoted_order, monkeypatch):
    from billing import services as billing

    calls = []
    monkeypatch.setattr(billing, "start_payment", lambda order, phone, client=None: calls.append((order.pk, phone)))
    logged_in.post(reverse("orders:pay", args=[quoted_order.public_id]), {"theme": "slate", "phone": "0712345678"})
    quoted_order.refresh_from_db()
    assert calls == [(quoted_order.pk, "0712345678")] and quoted_order.theme.slug == "slate"


def test_pay_shows_phone_errors(logged_in, quoted_order):
    resp = logged_in.post(reverse("orders:pay", args=[quoted_order.public_id]),
                          {"theme": "navy", "phone": "123"}, follow=True)
    assert "Enter a phone number like 0712 345 678" in resp.content.decode()


def test_status_poll_refreshes_the_page_when_status_changes(logged_in, quoted_order):
    url = reverse("orders:status", args=[quoted_order.public_id])
    same = logged_in.get(url, {"from": "quoted"})
    changed = logged_in.get(url, {"from": "payment_pending"})
    assert same.status_code == 200 and "HX-Refresh" not in same
    assert changed.status_code == 204 and changed["HX-Refresh"] == "true"


def test_done_order_offers_download_and_rerun(logged_in, done_order):
    html = logged_in.get(reverse("orders:detail", args=[done_order.public_id])).content.decode()
    assert "Your deck is ready" in html and "Redo in Clean Slate" in html


def test_download_serves_the_polished_file_with_a_friendly_name(logged_in, done_order):
    resp = logged_in.get(reverse("orders:download", args=[done_order.public_id]))
    assert resp.status_code == 302
    file_resp = logged_in.get(resp["Location"])
    assert file_resp.status_code == 200
    assert 'filename="Board pack (polished).pptx"' in file_resp["Content-Disposition"]


def test_tampered_file_links_are_rejected(logged_in, done_order):
    resp = logged_in.get(reverse("orders:download", args=[done_order.public_id]))
    assert logged_in.get(resp["Location"][:-3] + "abc/").status_code == 404


def test_rerun_needs_a_different_theme(logged_in, done_order):
    resp = logged_in.post(reverse("orders:rerun", args=[done_order.public_id]), {"theme": "navy"}, follow=True)
    assert "Choose a different theme" in resp.content.decode()


def test_simulated_payment_is_unavailable_outside_debug(logged_in, quoted_order):
    assert logged_in.post(reverse("orders:dev_pay", args=[quoted_order.public_id])).status_code == 404


def test_legal_pages_show_a_draft_banner_until_reviewed(client, db, settings):
    assert "Draft" in client.get(reverse("privacy")).content.decode()
    settings.LEGAL_PAGES_REVIEWED = True
    assert "Draft" not in client.get(reverse("terms")).content.decode()


def test_signup_logs_in(client, db):
    resp = client.post(reverse("signup"), {"email": "New@Example.com", "password1": "kahawa-tamu-2026",
                                           "password2": "kahawa-tamu-2026"})
    assert resp.status_code == 302
    assert User.objects.get().email == "new@example.com"


def test_admin_order_list_shows_cost_and_margin(client, done_order):
    admin = User.objects.create_superuser("admin@example.com", "admin-password-7")
    client.force_login(admin)
    html = client.get("/admin/orders/order/").content.decode()
    assert "AI cost (KES)" in html and "Margin (KES)" in html and "Board pack.pptx" in html


def test_admin_daily_spend_page_shows_the_cap(client, db):
    from orders import spend

    spend.record_spend(0.5)
    admin = User.objects.create_superuser("admin2@example.com", "admin-password-7")
    client.force_login(admin)
    resp = client.get("/admin/orders/dailyspend/")
    assert resp.status_code == 200 and "Cap (USD)" in resp.content.decode()


def test_admin_margin_subtracts_logged_payment_fees(client, done_order):
    from decimal import Decimal

    from billing.models import Payment

    Payment.objects.create(order=done_order, provider="intasend", provider_ref="INV-9", phone="254712345678",
                           amount_kes=200, fee_kes=Decimal("9.00"), status="paid")
    admin = User.objects.create_superuser("admin3@example.com", "admin-password-7")
    client.force_login(admin)
    html = client.get("/admin/orders/order/").content.decode()
    assert "Payment fees (KES)" in html and "9.00" in html and "191.00" in html


def test_quote_lists_locked_objects_and_warns_about_links(logged_in, tmp_path):
    from lockedbuilder import build_locked_deck

    data = build_locked_deck(tmp_path / "locked.pptx").read_bytes()
    html = logged_in.post(reverse("orders:upload"), {"deck": pptx(data)}, follow=True).content.decode()
    assert "These are kept exactly as they are, and the slides are restyled around them." in html
    assert "links to external files that may not open on other computers" in html


def test_preview_page_notes_equation_rendering(logged_in, user, tmp_path, monkeypatch):
    from lockedbuilder import build_locked_deck
    from orders import processing

    def fake_previews(pptx_path, out_dir, dpi=60):
        png = Path(out_dir) / "slide-1.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"\x89PNG fake")
        return [png]

    monkeypatch.setattr(processing, "render_previews", fake_previews)
    order = services.create_order(user, pptx(build_locked_deck(tmp_path / "locked.pptx").read_bytes()))
    order.transition(S.PAYMENT_PENDING)
    order.transition(S.PAID)
    tasks.process_order(order.pk)
    order.refresh_from_db()
    assert order.status == S.DONE, order.failure_reason
    html = logged_in.get(reverse("orders:detail", args=[order.public_id])).content.decode()
    assert "equations" in html and "doesn't affect your download" in html
