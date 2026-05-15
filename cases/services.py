"""
cases/services.py
Business logic for email alerts, notification creation, and reminder dispatch.
Called from views (on save) and from the management command (scheduled reminders).
"""
from django.core.mail import send_mail, EmailMultiAlternatives
from django.conf import settings
from django.utils import timezone
from django.template.loader import render_to_string


# ── Internal notification helpers ─────────────────────────────────────────

def notify_user(user, notif_type, title, message='', case=None, link=''):
    """Create a single in-app Notification for one user."""
    from .models import Notification
    Notification.objects.create(
        user=user, notif_type=notif_type, title=title,
        message=message, case=case,
        link=link or (f'/cases/{case.pk}/' if case else ''),
    )


def notify_team_about_task(task):
    """Notify the assigned user that a task was created for them."""
    if task.assigned_to:
        notify_user(
            user=task.assigned_to,
            notif_type='task',
            title=f'New task: {task.title}',
            message=f'On case {task.case.case_number} — {task.case.title}',
            case=task.case,
        )


def notify_deadline_created(deadline):
    """Notify assigned lawyer about a new deadline."""
    if deadline.case.assigned_to:
        notify_user(
            user=deadline.case.assigned_to,
            notif_type='deadline',
            title=f'Deadline added: {deadline.title}',
            message=f'Due {deadline.due_date.strftime("%d %b %Y")} on case {deadline.case.case_number}',
            case=deadline.case,
        )


# ── Email helpers ──────────────────────────────────────────────────────────

def _build_hearing_email(case, subject_prefix='Hearing Alert'):
    """Return (subject, plain_body, html_body) for a hearing alert."""
    subject = f'{subject_prefix}: {case.title} — {case.next_hearing_date.strftime("%d %b %Y")}'
    site    = getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000')
    case_url = f'{site}/cases/{case.pk}/'

    plain = (
        f'Hearing Alert — {settings.SITE_NAME}\n\n'
        f'Case: {case.title}\n'
        f'Case No: {case.case_number}\n'
        f'Court: {case.court_name}\n'
        f'Hearing Date: {case.next_hearing_date.strftime("%d %B %Y")}\n'
        f'Status: {case.get_status_display()}\n'
        f'Assigned: {case.assigned_to.get_full_name() if case.assigned_to else "—"}\n\n'
        f'View case: {case_url}\n\n'
        f'— {settings.SITE_NAME}'
    )

    days = case.days_to_hearing
    urgency_label = 'TODAY' if days == 0 else ('TOMORROW' if days == 1 else f'in {days} days')
    urgency_color = '#dc2626' if days == 0 else ('#d97706' if days == 1 else '#0f1b35')

    html = f"""
    <div style="font-family:sans-serif;max-width:540px;margin:0 auto;color:#1a1a2e">
      <div style="background:#0f1b35;padding:24px 28px;border-radius:12px 12px 0 0">
        <h1 style="color:white;font-size:20px;margin:0">⚖ {settings.SITE_NAME}</h1>
        <p style="color:rgba(255,255,255,0.6);font-size:13px;margin:4px 0 0">Hearing Alert</p>
      </div>
      <div style="background:white;border:1px solid #e5e7eb;border-top:none;padding:28px;border-radius:0 0 12px 12px">
        <div style="background:{urgency_color};color:white;border-radius:8px;padding:12px 18px;margin-bottom:20px;font-weight:600;font-size:16px">
          📅 Hearing {urgency_label}: {case.next_hearing_date.strftime("%d %B %Y")}
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <tr><td style="color:#6b7280;padding:7px 0;width:120px">Case</td><td style="font-weight:600">{case.title}</td></tr>
          <tr><td style="color:#6b7280;padding:7px 0">Case No.</td><td>{case.case_number}</td></tr>
          <tr><td style="color:#6b7280;padding:7px 0">Court</td><td>{case.court_name}</td></tr>
          <tr><td style="color:#6b7280;padding:7px 0">Parties</td><td>{case.petitioner or '—'} vs {case.respondent or '—'}</td></tr>
          <tr><td style="color:#6b7280;padding:7px 0">Lawyer</td><td>{case.assigned_to.get_full_name() if case.assigned_to else '—'}</td></tr>
        </table>
        <div style="margin-top:24px">
          <a href="{case_url}" style="background:#0f1b35;color:white;padding:11px 22px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600">View Case →</a>
        </div>
        <p style="margin-top:24px;font-size:12px;color:#9ca3af">
          You are receiving this because you are listed as a recipient for case {case.case_number}.<br>
          Sent by {settings.SITE_NAME}
        </p>
      </div>
    </div>"""
    return subject, plain, html


def send_hearing_alert_to_recipients(case, trigger='manual'):
    """
    Send hearing alert emails to all active HearingEmailRecipients on a case.
    Returns (sent_count, failed_list).
    """
    if not case.next_hearing_date:
        return 0, []

    recipient_emails = _get_case_alert_emails(case)
    if not recipient_emails:
        return 0, []

    prefix = 'Hearing Reminder' if trigger == 'reminder' else 'Hearing Alert'
    subject, plain, html = _build_hearing_email(case, prefix)
    sent = 0
    failed = []

    for email in recipient_emails:
        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=plain,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[email],
            )
            msg.attach_alternative(html, 'text/html')
            msg.send()
            sent += 1
        except Exception as e:
            failed.append(f'{email}: {e}')

    return sent, failed


def send_hearing_alert_to_emails(case, email_list):
    """Send hearing alert to an ad-hoc list of emails (from sync modal)."""
    if not case.next_hearing_date or not email_list:
        return 0, []
    days = case.days_to_hearing
    trigger = 'manual'
    subject_prefix = 'Hearing Invite'
    if days == 7:
        trigger = 'seven_day'
        subject_prefix = 'Hearing in 7 Days'
    elif days == 1:
        trigger = 'next_day'
        subject_prefix = 'Hearing Tomorrow'
    elif days == 0:
        trigger = 'same_day'
        subject_prefix = '🚨 Hearing TODAY'
    sent = 0
    failed = []
    for email in email_list:
        try:
            subject, plain, html = _build_hearing_email_with_optout(
                case, email, subject_prefix, trigger
            )
            msg = EmailMultiAlternatives(subject=subject, body=plain,
                from_email=settings.DEFAULT_FROM_EMAIL, to=[email])
            msg.attach_alternative(html, 'text/html')
            msg.send()
            sent += 1
        except Exception as e:
            failed.append(f'{email}: {e}')
    return sent, failed


# ── Auto-trigger on case save ─────────────────────────────────────────────

def on_hearing_date_changed(case, old_date):
    """
    Called when next_hearing_date changes on a case.
    Sends alerts to stored recipients and notifies assigned lawyer.
    """
    send_hearing_alert_to_recipients(case, trigger='change')

    if case.assigned_to:
        notify_user(
            user=case.assigned_to,
            notif_type='hearing',
            title=f'Hearing date updated: {case.next_hearing_date.strftime("%d %b %Y")}',
            message=f'{case.title} — {case.court_name}',
            case=case,
        )


# ── Scheduled reminder dispatch (called from management command) ───────────

def dispatch_hearing_reminders():
    """
    Send reminder emails for cases with hearings in HEARING_REMINDER_DAYS.
    Should be called daily via cron or Celery beat.
    Returns total emails sent.
    """
    from .models import Case
    reminder_days = getattr(settings, 'HEARING_REMINDER_DAYS', [7, 1])
    today = timezone.now().date()
    total_sent = 0

    for days in reminder_days:
        target = today + timezone.timedelta(days=days)
        cases = Case.objects.filter(
            next_hearing_date=target,
            status__in=['in_progress', 'on_hold'],
        )
        for case in cases:
            sent, _ = send_hearing_alert_to_recipients(case, trigger='reminder')
            total_sent += sent

            # Also create in-app notification for assigned lawyer
            if case.assigned_to:
                notify_user(
                    user=case.assigned_to,
                    notif_type='hearing',
                    title=f'Hearing in {days} day{"s" if days > 1 else ""}: {case.case_number}',
                    message=f'{case.court_name} — {case.next_hearing_date.strftime("%d %b %Y")}',
                    case=case,
                )

    return total_sent


def dispatch_overdue_notifications():
    """
    Create in-app notifications for overdue deadlines and tasks.
    Run daily. Skips duplicates created today.
    """
    from .models import CaseDeadline, CaseTask, Notification
    today = timezone.now().date()

    # Overdue deadlines
    overdue_dl = CaseDeadline.objects.filter(
        due_date__lt=today, is_completed=False
    ).select_related('case', 'case__assigned_to')

    for dl in overdue_dl:
        user = dl.case.assigned_to
        if not user:
            continue
        # Avoid duplicate: one per deadline per day
        already = Notification.objects.filter(
            user=user, notif_type='overdue',
            title__contains=dl.title,
            created_at__date=today,
        ).exists()
        if not already:
            notify_user(
                user=user,
                notif_type='overdue',
                title=f'Overdue deadline: {dl.title}',
                message=f'Due {dl.due_date.strftime("%d %b %Y")} on {dl.case.case_number}',
                case=dl.case,
            )

    # Overdue tasks
    overdue_tasks = CaseTask.objects.filter(
        due_date__lt=today, is_completed=False
    ).select_related('case', 'assigned_to')

    for task in overdue_tasks:
        user = task.assigned_to
        if not user:
            continue
        already = Notification.objects.filter(
            user=user, notif_type='overdue',
            title__contains=task.title,
            created_at__date=today,
        ).exists()
        if not already:
            notify_user(
                user=user,
                notif_type='overdue',
                title=f'Overdue task: {task.title}',
                message=f'Was due {task.due_date.strftime("%d %b %Y")} on {task.case.case_number}',
                case=task.case,
            )


# ── Opt-out token generation ──────────────────────────────────────────────────

def _make_opt_out_token(case, email, hearing_date):
    """Generate a secure, deterministic token for the opt-out link."""
    import hashlib, hmac
    from django.conf import settings
    payload = f"{case.pk}:{email}:{hearing_date}"
    return hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _get_or_create_opt_out(case, email):
    """Get or create an opt-out record for this email+case+hearing_date."""
    from .models import HearingAlertOptOut
    if not case.next_hearing_date:
        return None
    token = _make_opt_out_token(case, email, case.next_hearing_date)
    obj, _ = HearingAlertOptOut.objects.get_or_create(
        case=case, email=email, hearing_date=case.next_hearing_date,
        defaults={'token': token}
    )
    # Always refresh token in case it's stale
    if obj.token != token:
        obj.token = token
        obj.save(update_fields=['token'])
    return obj


def _is_opted_out(case, email):
    """Return True if this email has actively opted out of 7-day reminders."""
    from .models import HearingAlertOptOut
    if not case.next_hearing_date:
        return False
    return HearingAlertOptOut.objects.filter(
        case=case, email=email,
        hearing_date=case.next_hearing_date,
        is_active=True,
        opted_out_at__isnull=False,
    ).exists()


def _get_case_alert_emails(case):
    """
    Merge case-specific and global recipient lists into one deduplicated list.
    """
    from .models import GlobalAlertRecipient

    case_emails = list(
        case.email_recipients.filter(is_active=True).values_list('email', flat=True)
    )
    global_emails = list(
        GlobalAlertRecipient.objects.filter(is_active=True).values_list('email', flat=True)
    )
    merged = []
    seen = set()
    for email in case_emails + global_emails:
        e = (email or '').strip().lower()
        if not e or e in seen:
            continue
        seen.add(e)
        merged.append(e)
    return merged


# ── Enhanced hearing email with opt-out link ─────────────────────────────────

def _build_hearing_email_with_optout(case, email, subject_prefix, trigger):
    """
    Builds hearing alert email HTML/text.
    For 7-day reminders: includes a 'Stop Alerts' link.
    For same-day/next-day: urgent styling, no stop link.
    """
    subject, plain, html = _build_hearing_email(case, subject_prefix)
    site = getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000')

    is_urgent = trigger in ('same_day', 'next_day')
    days = case.days_to_hearing

    if is_urgent:
        urgent_banner = (
            '<div style="background:#dc2626;color:white;border-radius:8px;padding:14px 18px;'
            'margin-bottom:16px;font-weight:700;font-size:17px;text-align:center">'
            f'🚨 {"TODAY" if days == 0 else "TOMORROW"} — Hearing Reminder</div>'
        )
        html = html.replace('📅', '🚨').replace(
            '</div>\n      <div style="background:white',
            '</div>\n' + urgent_banner + '\n      <div style="background:white'
        )
    elif trigger == 'seven_day':
        # Generate opt-out link
        opt_out = _get_or_create_opt_out(case, email)
        if opt_out:
            stop_url = f"{site}/alerts/stop/{opt_out.token}/"
            plain += (
                "\n\nStop 7-day reminders (you will still receive final 1-day reminder):\n"
                f"{stop_url}\n"
            )
            stop_block = (
                f'<div style="margin-top:20px;padding:12px 16px;background:#f9fafb;'
                f'border:1px solid #e5e7eb;border-radius:8px;font-size:12px;color:#6b7280">'
                f'<strong>Already aware of this hearing?</strong><br>'
                f'<a href="{stop_url}" style="color:#dc2626;font-weight:600">'
                f'Stop 7-day reminders for this case →</a><br>'
                f'<span style="font-size:11px">You will still receive a final reminder the day before the hearing.</span>'
                f'</div>'
            )
            html = html.replace('</div>\n    </div>', stop_block + '\n</div>\n    </div>', 1)

    return subject, plain, html


def dispatch_hearing_reminders():
    """
    Enhanced dispatch:
    - 7 days before  → reminder with opt-out link (skip if opted out)
    - 1 day before   → final reminder (always sent, no opt-out)
    - Same day       → urgent alert (always sent)
    """
    from .models import Case
    today = timezone.now().date()
    total_sent = 0

    # --- 7-day reminders (with opt-out stop link) ---
    for case in Case.objects.filter(
        next_hearing_date=today + timezone.timedelta(days=7),
        status__in=['in_progress', 'on_hold'],
    ):
        for email in _get_case_alert_emails(case):
            if _is_opted_out(case, email):
                continue
            subject, plain, html = _build_hearing_email_with_optout(
                case, email, 'Hearing in 7 Days', 'seven_day'
            )
            if _send_one(email, subject, plain, html):
                total_sent += 1
        _notify_lawyer(case, 7)

    # --- Next-day final alert (always sent; ignore prior opt-out) ---
    for case in Case.objects.filter(
        next_hearing_date=today + timezone.timedelta(days=1),
        status__in=['in_progress', 'on_hold'],
    ):
        for email in _get_case_alert_emails(case):
            subject, plain, html = _build_hearing_email_with_optout(
                case, email, 'Hearing Tomorrow', 'next_day'
            )
            if _send_one(email, subject, plain, html):
                total_sent += 1
        _notify_lawyer(case, 1)

    # --- Same-day urgent alert (always sent; ignore prior opt-out) ---
    for case in Case.objects.filter(
        next_hearing_date=today,
        status__in=['in_progress', 'on_hold'],
    ):
        for email in _get_case_alert_emails(case):
            subject, plain, html = _build_hearing_email_with_optout(
                case, email, '🚨 Hearing TODAY', 'same_day'
            )
            if _send_one(email, subject, plain, html):
                total_sent += 1
        _notify_lawyer(case, 0)

    return total_sent


def _send_one(email, subject, plain, html):
    try:
        from django.core.mail import EmailMultiAlternatives
        msg = EmailMultiAlternatives(subject, plain, settings.DEFAULT_FROM_EMAIL, [email])
        msg.attach_alternative(html, 'text/html')
        msg.send()
        return True
    except Exception:
        return False


def _notify_lawyer(case, days):
    if not case.assigned_to:
        return
    if days == 0:
        title = f'🚨 Hearing TODAY: {case.case_number}'
    elif days == 1:
        title = f'Hearing TOMORROW: {case.case_number}'
    else:
        title = f'Hearing in {days} days: {case.case_number}'
    notify_user(
        user=case.assigned_to,
        notif_type='hearing',
        title=title,
        message=f'{case.court_name} — {case.next_hearing_date.strftime("%d %b %Y")}',
        case=case,
    )
