from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    """Amendment 1A: IntaSend replaces Daraja; payment fields become provider-neutral."""

    dependencies = [("billing", "0002_initial")]

    operations = [
        migrations.RenameField("payment", "checkout_request_id", "provider_ref"),
        migrations.RenameField("payment", "mpesa_receipt", "provider_receipt"),
        migrations.AlterField(
            "payment", "provider_ref",
            models.CharField(blank=True, help_text="The provider's payment ID", max_length=64, null=True),
        ),
        migrations.AlterField(
            "payment", "provider_receipt",
            models.CharField(blank=True, help_text="M-Pesa receipt number", max_length=64),
        ),
        migrations.RemoveField("payment", "merchant_request_id"),
        migrations.RemoveField("payment", "raw_callback"),
        migrations.AddField("payment", "provider", models.CharField(default="intasend", max_length=20)),
        migrations.AddField(
            "payment", "reference",
            models.CharField(blank=True, help_text="Our reference sent to the provider", max_length=64),
        ),
        migrations.AddField(
            "payment", "fee_kes",
            models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=10, verbose_name="fee (KES)"),
        ),
        migrations.AddField(
            "payment", "fee_estimated",
            models.BooleanField(default=False, help_text="The provider didn't report its fee; estimated"),
        ),
        migrations.AddField("payment", "raw_init", models.JSONField(blank=True, null=True)),
        migrations.AddField("payment", "raw_callbacks", models.JSONField(blank=True, default=list)),
        migrations.AlterField("payment", "result_code", models.CharField(blank=True, max_length=32)),
        migrations.AddConstraint(
            "payment", models.UniqueConstraint(fields=("provider", "provider_ref"), name="unique_provider_ref")
        ),
    ]
