"""
cases/signals.py
Auto-logs every meaningful change to a Case into CaseHistory.
Tracks: creation, field edits, status changes, reassignments, hearing dates.
Also provides helper functions called from views for non-field events.
"""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone


# ── Fields we track automatically ─────────────────────────────────────────────
# Maps field_name → human-readable label
TRACKED_FIELDS = {
    'title':             'Title',
    'court_name':        'Court',
    'case_type':         'Case Type',
    'status':            'Status',
    'assigned_to_id':    'Assigned Lawyer',
    'next_hearing_date': 'Next Hearing Date',
    'last_hearing_date': 'Last Hearing Date',
    'filing_date':       'Filing Date',
    'petitioner':        'Petitioner',
    'respondent':        'Respondent',
    'project':           'Project',
    'description':       'Description',
}


def _display(instance, field):
    """Return human-readable value for a field on a Case instance."""
    if field == 'status':
        return dict(instance.STATUS_CHOICES).get(
            getattr(instance, field, ''), getattr(instance, field, ''))
    if field == 'case_type':
        return dict(instance.CASE_TYPE_CHOICES).get(
            getattr(instance, field, ''), getattr(instance, field, ''))
    if field == 'assigned_to_id':
        uid = getattr(instance, 'assigned_to_id', None)
        if uid:
            try:
                from accounts.models import User
                u = User.objects.get(pk=uid)
                return u.get_full_name() or u.username
            except Exception:
                return str(uid)
        return '—'
    val = getattr(instance, field, None)
    if val is None:
        return '—'
    if hasattr(val, 'strftime'):
        return val.strftime('%d %b %Y')
    return str(val) if val else '—'


# Store pre-save snapshots here: {pk: {field: value}}
_pre_save_snapshots = {}


@receiver(pre_save, sender='cases.Case')
def case_pre_save(sender, instance, **kwargs):
    """Capture field values BEFORE save so we can diff them after."""
    if instance.pk:
        try:
            old = sender.objects.get(pk=instance.pk)
            _pre_save_snapshots[instance.pk] = {
                f: getattr(old, f) for f in TRACKED_FIELDS
            }
        except sender.DoesNotExist:
            pass


@receiver(post_save, sender='cases.Case')
def case_post_save(sender, instance, created, **kwargs):
    """After save: log creation or each changed field."""
    from .models import CaseHistory

    # Get the user from thread-local if available
    user = getattr(instance, '_history_user', None)

    if created:
        CaseHistory.log(
            case=instance, action='created', changed_by=user,
            description=f'Case {instance.case_number} created — {instance.title}',
        )
        _pre_save_snapshots.pop(instance.pk, None)
        return

    old_snap = _pre_save_snapshots.pop(instance.pk, {})
    if not old_snap:
        return
    hearing_date_changed = False
    old_hearing_date = old_snap.get('next_hearing_date')

    for field, label in TRACKED_FIELDS.items():
        old_val = old_snap.get(field)
        new_val = getattr(instance, field, None)

        # Normalise for comparison
        old_str = str(old_val) if old_val is not None else ''
        new_str = str(new_val) if new_val is not None else ''

        if old_str == new_str:
            continue  # No change

        # Determine action type
        if field == 'status':
            action = 'status'
        elif field == 'assigned_to_id':
            action = 'assigned'
        elif field == 'next_hearing_date':
            action = 'hearing'
        else:
            action = 'edited'

        old_display = _display_raw(instance, field, old_val)
        new_display = _display_raw(instance, field, new_val)

        CaseHistory.log(
            case=instance, action=action, changed_by=user,
            field_name=label,
            old_value=old_display,
            new_value=new_display,
            description=f'{label} changed from "{old_display}" to "{new_display}"',
        )
        if field == 'next_hearing_date':
            hearing_date_changed = True

    # Trigger automatic hearing alerts when hearing date is updated.
    if hearing_date_changed and instance.next_hearing_date:
        try:
            from .services import on_hearing_date_changed
            on_hearing_date_changed(instance, old_hearing_date)
        except Exception:
            # Non-fatal: never block case save due to alert delivery.
            pass


def _display_raw(instance, field, raw_val):
    """Return human-readable label for a raw DB value."""
    if field == 'status' and raw_val:
        return dict(instance.STATUS_CHOICES).get(str(raw_val), str(raw_val))
    if field == 'case_type' and raw_val:
        return dict(instance.CASE_TYPE_CHOICES).get(str(raw_val), str(raw_val))
    if field == 'assigned_to_id' and raw_val:
        try:
            from accounts.models import User
            u = User.objects.get(pk=raw_val)
            return u.get_full_name() or u.username
        except Exception:
            return str(raw_val)
    if raw_val is None or raw_val == '':
        return '—'
    if hasattr(raw_val, 'strftime'):
        return raw_val.strftime('%d %b %Y')
    return str(raw_val)


# ── Helper functions called from views ────────────────────────────────────────

def log_document_event(case, action_desc, user=None):
    from .models import CaseHistory
    CaseHistory.log(case=case, action='document', changed_by=user,
                    description=action_desc)


def log_update_event(case, update_title, user=None):
    from .models import CaseHistory
    CaseHistory.log(case=case, action='update', changed_by=user,
                    description=f'Case update added: "{update_title}"')


def log_task_event(case, task_title, event_type='added', user=None):
    from .models import CaseHistory
    desc = f'Task {event_type}: "{task_title}"'
    CaseHistory.log(case=case, action='task', changed_by=user, description=desc)


def log_deadline_event(case, deadline_title, event_type='added', user=None):
    from .models import CaseHistory
    desc = f'Deadline {event_type}: "{deadline_title}"'
    CaseHistory.log(case=case, action='deadline', changed_by=user, description=desc)


def log_outcome_event(case, result, user=None, judge_name='', opposing_counsel=''):
    from .models import CaseHistory
    desc_parts = [f'Outcome recorded: {result}']
    if judge_name:
        desc_parts.append(f'Judge / bench: {judge_name}')
    if opposing_counsel:
        desc_parts.append(f'Opposing counsel: {opposing_counsel}')
    CaseHistory.log(
        case=case, action='outcome', changed_by=user,
        description=' · '.join(desc_parts),
    )


def log_court_order_link_event(case, link, user=None):
    """Log addition of a court order PDF URL to the case change log."""
    from .models import CaseHistory

    label = link.link_display_text()
    od = link.order_date.strftime('%d/%m/%Y') if link.order_date else '—'
    extra = f' · Judge: {link.judge_name}' if (link.judge_name or '').strip() else ''
    CaseHistory.log(
        case=case,
        action='court_order',
        changed_by=user,
        description=f'Order PDF added ({od}): {label}{extra}',
    )


def log_hearing_record_event(case, hearing, user=None):
    """Log a saved HearingRecord into the case change log (includes judge if set)."""
    from .models import CaseHistory

    date_s = hearing.hearing_date.strftime('%d %b %Y') if hearing.hearing_date else '—'
    desc_parts = [f'Hearing on {date_s}: {hearing.purpose or "—"}']
    if hearing.judge_name:
        desc_parts.append(f'Judge / bench: {hearing.judge_name}')
    CaseHistory.log(
        case=case,
        action='hearing_record',
        changed_by=user,
        description=' — '.join(desc_parts),
    )
