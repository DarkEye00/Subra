"""Amendment 2: map pay-first statuses onto the generate-first lifecycle, and fill in paid_at."""

from django.db import migrations

OLD_TO_NEW = {
    "uploaded": "quoted", "quoted": "quoted", "payment_pending": "quoted", "payment_failed": "quoted",
    "paid": "queued", "processing": "queued", "done": "ready",
    "failed": "failed", "refund_pending": "failed", "refunded": "failed", "expired": "expired",
}


def forwards(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    Payment = apps.get_model("billing", "Payment")
    for order in Order.objects.all():
        old = order.status
        order.status = OLD_TO_NEW.get(old, old)
        first_paid = Payment.objects.filter(order_id=order.pk, status="paid").order_by("updated_at").first()
        if first_paid is not None:
            order.paid_at = first_paid.updated_at
        order.save(update_fields=["status", "paid_at"])
        if old == "refunded":
            Payment.objects.filter(order_id=order.pk, status="paid").update(status="refunded")


class Migration(migrations.Migration):
    dependencies = [("orders", "0004_pay_after_generation"), ("billing", "0004_pay_after_generation")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
