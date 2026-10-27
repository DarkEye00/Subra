from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from billing.lifecycle import run_daily, run_hourly


class Command(BaseCommand):
    help = "Run the hourly subscription jobs (and the daily ones with --daily), as Celery beat would."

    def add_arguments(self, parser):
        parser.add_argument("--now", help="Pretend it's this time (ISO 8601, e.g. 2026-11-04T09:00:00+03:00)")
        parser.add_argument("--daily", action="store_true", help="Also run the daily jobs")

    def handle(self, *args, now=None, daily=False, **options):
        when = None
        if now:
            when = parse_datetime(now)
            if when is None or when.tzinfo is None:
                raise CommandError("--now must be an ISO datetime with a timezone")
        self.stdout.write(f"hourly: {run_hourly(now=when)}")
        if daily:
            self.stdout.write(f"daily: {run_daily(now=when)}")
