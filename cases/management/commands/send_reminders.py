"""
Management command: send_reminders
Run daily via cron: python manage.py send_reminders

Add to crontab:
  0 8 * * * /path/to/venv/bin/python /path/to/manage.py send_reminders
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Send hearing reminder emails and create overdue notifications'

    def handle(self, *args, **options):
        from cases.services import dispatch_hearing_reminders, dispatch_overdue_notifications

        self.stdout.write(f'\n📬 Running reminders — {timezone.now().strftime("%d %b %Y %H:%M")}')

        sent = dispatch_hearing_reminders()
        self.stdout.write(self.style.SUCCESS(f'  ✓ Hearing reminder emails sent: {sent}'))

        dispatch_overdue_notifications()
        self.stdout.write(self.style.SUCCESS(f'  ✓ Overdue notifications dispatched'))

        self.stdout.write(self.style.SUCCESS('Done.\n'))
