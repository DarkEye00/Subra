import pytest
from django.contrib import admin

from accounts.models import User
from orders import spend
from orders.admin import OrderAdmin
from orders.models import Order


@pytest.fixture
def staff_client(client, db):
    client.force_login(User.objects.create_superuser("admin@example.com", "admin-password-7"))
    return client


def test_order_list_shows_paid_cost_and_margin(staff_client, paid_order):
    html = staff_client.get("/admin/orders/order/").content.decode()
    assert "AI cost (KES)" in html and "Margin (KES)" in html and "Q3 review.pptx" in html
    assert "197.00" in html  # KES 200 - AI 0.00 (heuristic planner) - fee 3.00


def test_margin_is_only_shown_for_paid_orders(ready_order):
    assert OrderAdmin(Order, admin.site).margin(ready_order) == "-"


def test_orders_can_be_filtered_by_payment(staff_client, paid_order, order_factory):
    order_factory("Unpaid.pptx", status=Order.Status.READY)
    paid = staff_client.get("/admin/orders/order/?paid=yes").content.decode()
    unpaid = staff_client.get("/admin/orders/order/?paid=no").content.decode()
    assert "Q3 review.pptx" in paid and "Unpaid.pptx" not in paid
    assert "Unpaid.pptx" in unpaid and "Q3 review.pptx" not in unpaid


def test_daily_spend_page_shows_the_cap(staff_client):
    spend.record_spend(0.5)
    resp = staff_client.get("/admin/orders/dailyspend/")
    assert resp.status_code == 200 and "Cap (USD)" in resp.content.decode()


def test_payments_can_be_marked_refunded(staff_client, paid_order):
    payment = paid_order.payments.get()
    staff_client.post("/admin/billing/payment/", {"action": "mark_refunded", "_selected_action": [payment.pk]})
    payment.refresh_from_db()
    assert payment.status == "refunded" and payment.refunded_at


def test_month_summary_splits_ai_cost_by_payment(paid_order, order_factory):
    from decimal import Decimal

    from orders.models import AIJob, OrderEvent

    unpaid = order_factory("Unpaid.pptx", status=Order.Status.READY)
    OrderEvent.objects.create(order=unpaid, from_status="processing", to_status="ready")
    AIJob.objects.create(order=paid_order, attempt_number=2, cost_kes=Decimal("20.00"))
    AIJob.objects.create(order=unpaid, attempt_number=1, cost_kes=Decimal("30.00"))
    summary = spend.month_summary()
    assert (summary["paid_cost_kes"], summary["unpaid_cost_kes"]) == (Decimal("20.00"), Decimal("30.00"))
    assert (summary["generated"], summary["paid"], summary["paid_share"]) == (2, 1, 50)


def test_daily_spend_page_shows_the_month(staff_client, paid_order):
    html = staff_client.get("/admin/orders/dailyspend/").content.decode()
    assert "This month" in html and "Decks generated: 1, paid for: 1 (100%)" in html


def test_order_admin_shows_the_qa_report_and_ai_stages(staff_client, paid_order):
    html = staff_client.get(f"/admin/orders/order/{paid_order.pk}/change/").content.decode()
    assert "Qa report" in html and "retention" in html
    assert "Stage" in html
