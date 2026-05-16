"""
Cases app models — Case, CaseUpdate, CaseDocument, Comment,
                   CaseDeadline, CaseTask, HearingEmailRecipient, Notification
"""
import os
import re
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError


def validate_file_size(file):
    max_size = 10 * 1024 * 1024
    if file.size > max_size:
        raise ValidationError(f'File size cannot exceed 10MB. Current: {file.size/1024/1024:.1f}MB')


def document_upload_path(instance, filename):
    return f'documents/case_{instance.case.id}/{filename}'


class Case(models.Model):
    STATUS_CHOICES = [
        ('in_progress', 'In Progress'),
        ('disposed',    'Disposed'),
        ('on_hold',     'On Hold'),
    ]
    CASE_TYPE_CHOICES = [
        ('civil','Civil'),('criminal','Criminal'),('family','Family'),
        ('corporate','Corporate'),('property','Property'),('labour','Labour'),
        ('tax','Tax'),('constitutional','Constitutional'),('other','Other'),
    ]

    case_number       = models.CharField(max_length=50, unique=True)
    title             = models.CharField(max_length=255)
    project           = models.CharField(max_length=200, blank=True, null=True)
    court_name        = models.CharField(max_length=200)
    case_type         = models.CharField(max_length=50, choices=CASE_TYPE_CHOICES, default='civil')
    filing_date       = models.DateField()
    last_hearing_date = models.DateField(null=True, blank=True)
    next_hearing_date = models.DateField(null=True, blank=True)
    status            = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_progress')
    assigned_to       = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='assigned_cases', limit_choices_to={'role__in': ['lawyer','admin']}
    )
    description  = models.TextField(blank=True)
    petitioner   = models.CharField(max_length=255, blank=True)
    respondent   = models.CharField(max_length=255, blank=True)
    created_by   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, related_name='created_cases')
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Case'
        verbose_name_plural = 'Cases'

    def __str__(self):
        return f"{self.case_number} - {self.title}"

    @property
    def is_hearing_upcoming(self):
        if self.next_hearing_date:
            delta = (self.next_hearing_date - timezone.now().date()).days
            return 0 <= delta <= 30
        return False

    @property
    def days_to_hearing(self):
        if self.next_hearing_date:
            return (self.next_hearing_date - timezone.now().date()).days
        return None

    @property
    def status_badge_class(self):
        return {
            'in_progress': 'badge-inprogress',
            'disposed':    'badge-disposed',
            'on_hold':     'badge-hold',
        }.get(self.status, 'bg-secondary')

    @property
    def overdue_deadlines_count(self):
        return self.deadlines.filter(
            due_date__lt=timezone.now().date(), is_completed=False
        ).count()

    @property
    def pending_tasks_count(self):
        return self.tasks.filter(is_completed=False).count()


# ── Hearing Email Recipients ────────────────────────────────────────────────
class HearingEmailRecipient(models.Model):
    """Stores email addresses that receive automatic hearing alerts for a case."""
    case       = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='email_recipients')
    email      = models.EmailField()
    name       = models.CharField(max_length=100, blank=True, help_text='Label e.g. Client, Partner')
    added_by   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    added_at   = models.DateTimeField(auto_now_add=True)
    is_active  = models.BooleanField(default=True)

    class Meta:
        unique_together = ('case', 'email')
        ordering = ['name', 'email']
        verbose_name = 'Hearing Email Recipient'

    def __str__(self):
        return f"{self.email} ({self.case.case_number})"


class GlobalAlertRecipient(models.Model):
    """
    Email recipients who should receive hearing alerts for all cases.
    Useful for operations/management inboxes that must get every reminder.
    """
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=100, blank=True, help_text='Label e.g. Ops Team')
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    added_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name', 'email']
        verbose_name = 'Global Alert Recipient'
        verbose_name_plural = 'Global Alert Recipients'

    def __str__(self):
        return self.email


# ── Case Deadlines ───────────────────────────────────────────────────────────
class CaseDeadline(models.Model):
    """Filing deadlines, response deadlines, SLA milestones."""
    PRIORITY_CHOICES = [
        ('critical', 'Critical'),
        ('high',     'High'),
        ('medium',   'Medium'),
        ('low',      'Low'),
    ]
    case         = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='deadlines')
    title        = models.CharField(max_length=200)
    description  = models.TextField(blank=True)
    due_date     = models.DateField()
    priority     = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium')
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='completed_deadlines')
    created_by   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, related_name='created_deadlines')
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['due_date']
        verbose_name = 'Case Deadline'

    def __str__(self):
        return f"{self.case.case_number} — {self.title} ({self.due_date})"

    @property
    def days_remaining(self):
        if self.is_completed:
            return None
        return (self.due_date - timezone.now().date()).days

    @property
    def urgency_class(self):
        if self.is_completed:
            return 'completed'
        days = self.days_remaining
        if days is None:
            return ''
        if days < 0:
            return 'overdue'
        if days <= 3:
            return 'critical'
        if days <= 7:
            return 'warning'
        return 'ok'

    @property
    def priority_badge(self):
        return {
            'critical': 'badge-dl-critical',
            'high':     'badge-dl-high',
            'medium':   'badge-dl-medium',
            'low':      'badge-dl-low',
        }.get(self.priority, '')


# ── Case Tasks ───────────────────────────────────────────────────────────────
class CaseTask(models.Model):
    """Action items assigned to team members per case."""
    case         = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='tasks')
    title        = models.CharField(max_length=255)
    description  = models.TextField(blank=True)
    assigned_to  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, related_name='assigned_tasks')
    due_date     = models.DateField(null=True, blank=True)
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, related_name='created_tasks')
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['is_completed', 'due_date']
        verbose_name = 'Case Task'

    def __str__(self):
        return f"{self.case.case_number} — {self.title}"

    @property
    def is_overdue(self):
        return (not self.is_completed and self.due_date and
                self.due_date < timezone.now().date())


# ── Notifications ────────────────────────────────────────────────────────────
class Notification(models.Model):
    """In-app notifications for users."""
    TYPE_CHOICES = [
        ('hearing',  'Upcoming Hearing'),
        ('deadline', 'Deadline Due'),
        ('task',     'Task Assigned'),
        ('overdue',  'Overdue Item'),
        ('update',   'Case Updated'),
        ('system',   'System'),
    ]
    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                   related_name='notifications')
    notif_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='system')
    title      = models.CharField(max_length=200)
    message    = models.TextField(blank=True)
    case       = models.ForeignKey(Case, on_delete=models.CASCADE, null=True, blank=True,
                                   related_name='notifications')
    link       = models.CharField(max_length=300, blank=True)
    is_read    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Notification'

    def __str__(self):
        return f"[{self.notif_type}] {self.title} → {self.user}"

    @classmethod
    def create_for_user(cls, user, notif_type, title, message='', case=None, link=''):
        return cls.objects.create(
            user=user, notif_type=notif_type, title=title,
            message=message, case=case, link=link
        )


# ── CaseUpdate / CaseDocument / Comment (unchanged) ──────────────────────────
class CaseUpdate(models.Model):
    case             = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='updates')
    date             = models.DateField(default=timezone.now)
    title            = models.CharField(max_length=200)
    description      = models.TextField()
    order_details    = models.TextField(blank=True, help_text='Order details / key directions from court')
    filing_response  = models.TextField(blank=True, help_text='What was filed/submitted in response')
    response_received = models.TextField(blank=True, help_text='What response was received from court/opposite side')
    internal_notes   = models.TextField(blank=True, help_text='Internal case notes for this timeline entry')
    related_document = models.ForeignKey(
        'CaseDocument',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='timeline_updates',
        help_text='Legacy single document link (kept for compatibility)',
    )
    next_action_date = models.DateField(null=True, blank=True)
    created_by       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']
        verbose_name = 'Case Update'

    def __str__(self):
        return f"{self.case.case_number} - {self.title} ({self.date})"


class CaseUpdateDocumentAttachment(models.Model):
    """Multiple document attachments per timeline update."""
    case_update = models.ForeignKey(
        CaseUpdate, on_delete=models.CASCADE, related_name='attachments'
    )
    document = models.ForeignKey(
        'CaseDocument', on_delete=models.CASCADE, related_name='timeline_attachments'
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('case_update', 'document')
        ordering = ['-created_at']
        verbose_name = 'Case Update Document Attachment'

    def __str__(self):
        return f'{self.case_update_id} -> {self.document_id}'


class CaseDocument(models.Model):
    DOCUMENT_TYPE_CHOICES = [
        ('petition','Petition'),('evidence','Evidence'),('order','Court Order'),
        ('affidavit','Affidavit'),('notice','Notice'),('judgment','Judgment'),
        ('contract','Contract'),('correspondence','Correspondence'),('other','Other'),
    ]
    case          = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='documents')
    title         = models.CharField(max_length=200, blank=True)
    folder        = models.CharField(max_length=100, blank=True, help_text='Optional folder label e.g. Pleadings')
    tags          = models.CharField(max_length=300, blank=True, help_text='Comma-separated tags')
    document_type = models.CharField(max_length=50, choices=DOCUMENT_TYPE_CHOICES, default='other')
    file          = models.FileField(upload_to=document_upload_path, validators=[validate_file_size])
    uploaded_by   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    uploaded_at   = models.DateTimeField(auto_now_add=True)
    notes         = models.TextField(blank=True)

    class Meta:
        ordering = ['-uploaded_at']
        verbose_name = 'Case Document'
        db_table = 'case_documents'
        

    def __str__(self):
        return f"{self.case.case_number} - {self.get_document_type_display()}"

    @property
    def filename(self):
        return os.path.basename(self.file.name)

    @property
    def is_pdf(self):
        return self.filename.lower().endswith('.pdf')

    @property
    def file_extension(self):
        _, ext = os.path.splitext(self.filename)
        return ext.lower()

    @property
    def file_icon(self):
        return {
            '.pdf':'bi-file-pdf text-danger',
            '.doc':'bi-file-word text-primary','.docx':'bi-file-word text-primary',
            '.jpg':'bi-file-image text-success','.jpeg':'bi-file-image text-success',
            '.png':'bi-file-image text-success',
        }.get(self.file_extension, 'bi-file-earmark text-secondary')


class OfflineDocument(models.Model):
    """
    Central registry for offline/physical documents and optional digital copies.
    This is independent from case-detail uploads, but can be linked to a case.
    """
    case = models.ForeignKey(Case, on_delete=models.SET_NULL, null=True, blank=True, related_name='offline_documents')
    title = models.CharField(max_length=220)
    folder = models.CharField(max_length=100, blank=True, help_text='Optional folder label')
    tags = models.CharField(max_length=300, blank=True, help_text='Comma-separated tags')
    storage_location = models.CharField(
        max_length=300,
        blank=True,
        help_text='Physical location e.g. Rack A / Box 4 / Chamber file shelf',
    )
    file = models.FileField(upload_to='offline_documents/%Y/%m/', blank=True, null=True, validators=[validate_file_size])
    notes = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Offline Document'
        verbose_name_plural = 'Offline Documents'

    def __str__(self):
        return self.title

    @property
    def filename(self):
        if not self.file:
            return ''
        return os.path.basename(self.file.name)


class Comment(models.Model):
    case       = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='comments')
    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    comment    = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Comment'

    def __str__(self):
        return f"Comment by {self.user} on {self.case.case_number}"


# ══════════════════════════════════════════════════════════════════════
# PHASE 2 — Director Analytics models
# ══════════════════════════════════════════════════════════════════════

class CaseOutcome(models.Model):
    """
    Recorded when a case is disposed.
    Tracks win/loss/settlement for analytics and performance reporting.
    """
    OUTCOME_CHOICES = [
        ('won',       'Won'),
        ('lost',      'Lost'),
        ('settled',   'Settled'),
        ('withdrawn', 'Withdrawn'),
        ('dismissed', 'Dismissed'),
        ('transferred','Transferred'),
        ('other',     'Other'),
    ]
    case          = models.OneToOneField(Case, on_delete=models.CASCADE,
                                         related_name='outcome')
    result        = models.CharField(max_length=20, choices=OUTCOME_CHOICES)
    disposed_date = models.DateField(default=timezone.now)
    summary       = models.TextField(blank=True,
                    help_text='Brief summary of how the case was resolved')
    judge_name    = models.CharField(max_length=150, blank=True)
    opposing_counsel = models.CharField(max_length=150, blank=True)
    recorded_by   = models.ForeignKey(settings.AUTH_USER_MODEL,
                                      on_delete=models.SET_NULL, null=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Case Outcome'

    def __str__(self):
        return f"{self.case.case_number} — {self.get_result_display()}"

    @property
    def result_badge(self):
        return {
            'won':        'badge-outcome-won',
            'lost':       'badge-outcome-lost',
            'settled':    'badge-outcome-settled',
            'withdrawn':  'badge-outcome-withdrawn',
            'dismissed':  'badge-outcome-dismissed',
            'transferred':'badge-outcome-other',
            'other':      'badge-outcome-other',
        }.get(self.result, '')


class TimeEntry(models.Model):
    """
    Time log per case per lawyer.
    Used for billing summaries and effort tracking.
    """
    case        = models.ForeignKey(Case, on_delete=models.CASCADE,
                                    related_name='time_entries')
    user        = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL, null=True,
                                    related_name='time_entries')
    date        = models.DateField(default=timezone.now)
    hours       = models.DecimalField(max_digits=5, decimal_places=2,
                  help_text='Hours spent (e.g. 1.5 = 1h 30m)')
    description = models.CharField(max_length=300,
                  help_text='What was done (e.g. Drafted reply, Attended hearing)')
    is_billable = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']
        verbose_name = 'Time Entry'

    def __str__(self):
        return f"{self.case.case_number} — {self.user} — {self.hours}h ({self.date})"


# ══════════════════════════════════════════════════════════════════════
# PHASE 3 — Client Portal
# ══════════════════════════════════════════════════════════════════════

class ClientCase(models.Model):
    """
    Grants a client user read-only access to specific cases.
    Controls what they can see (documents, updates).
    """
    client              = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='accessible_cases',
        limit_choices_to={'role': 'client'}
    )
    case                = models.ForeignKey(Case, on_delete=models.CASCADE,
                                            related_name='client_accesses')
    added_by            = models.ForeignKey(settings.AUTH_USER_MODEL,
                                            on_delete=models.SET_NULL, null=True,
                                            related_name='client_grants')
    added_at            = models.DateTimeField(auto_now_add=True)
    can_view_documents  = models.BooleanField(default=True)
    can_view_updates    = models.BooleanField(default=True)

    class Meta:
        unique_together = ('client', 'case')
        verbose_name = 'Client Case Access'

    def __str__(self):
        return f"{self.client} → {self.case.case_number}"


# ══════════════════════════════════════════════════════════════════════
# CASE HISTORY — Auto-audit trail of every change made to a case
# ══════════════════════════════════════════════════════════════════════

class CaseHistory(models.Model):
    """
    Immutable audit log — one row per field change on a Case.
    Created automatically by a post_save signal. Never edited manually.

    Also stores explicit action events:
      'created'  — case was first created
      'edited'   — a field value changed (field_name, old_value, new_value filled)
      'status'   — status specifically changed (highlighted differently)
      'assigned' — assigned_to changed
      'hearing'  — next_hearing_date changed
      'hearing_record' — a HearingRecord row was added (includes judge when captured)
      'court_order' — a court order PDF URL was added to the order register
      'document' — document uploaded or deleted
      'update'   — a CaseUpdate was added
      'task'     — a task was added / completed
      'deadline' — a deadline was added / completed
      'outcome'  — case outcome recorded
      'note'     — manual note added by user (free-text)
    """
    ACTION_CHOICES = [
        ('created',  'Case Created'),
        ('edited',   'Field Changed'),
        ('status',   'Status Changed'),
        ('assigned', 'Lawyer Reassigned'),
        ('hearing',  'Hearing Date Changed'),
        ('hearing_record', 'Hearing Record Added'),
        ('court_order', 'Court Order PDF Added'),
        ('document', 'Document Event'),
        ('update',   'Case Update Added'),
        ('task',     'Task Event'),
        ('deadline', 'Deadline Event'),
        ('outcome',  'Outcome Recorded'),
        ('note',     'Manual Note'),
    ]

    case        = models.ForeignKey(Case, on_delete=models.CASCADE,
                                    related_name='history')
    action      = models.CharField(max_length=20, choices=ACTION_CHOICES)
    field_name  = models.CharField(max_length=100, blank=True,
                                   help_text='Which field changed (e.g. status, court_name)')
    old_value   = models.TextField(blank=True, help_text='Previous value')
    new_value   = models.TextField(blank=True, help_text='New value')
    description = models.TextField(blank=True,
                                   help_text='Human-readable summary of the event')
    changed_by  = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL, null=True,
                                    related_name='case_history_entries')
    changed_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-changed_at']
        verbose_name = 'Case History'
        verbose_name_plural = 'Case History'
        db_table = 'case_history'

    def __str__(self):
        return f"{self.case.case_number} [{self.action}] {self.changed_at:%d %b %Y %H:%M}"

    @property
    def action_icon(self):
        return {
            'created':  'bi-plus-circle-fill text-success',
            'edited':   'bi-pencil-fill text-secondary',
            'status':   'bi-arrow-left-right text-primary',
            'assigned': 'bi-person-fill-gear text-warning',
            'hearing':  'bi-calendar-event-fill text-danger',
            'hearing_record': 'bi-person-badge text-primary',
            'court_order': 'bi-file-earmark-pdf text-danger',
            'document': 'bi-file-earmark-fill text-info',
            'update':   'bi-clock-history text-secondary',
            'task':     'bi-check2-square text-success',
            'deadline': 'bi-flag-fill text-warning',
            'outcome':  'bi-trophy-fill text-success',
            'note':     'bi-sticky-fill text-warning',
        }.get(self.action, 'bi-circle text-muted')

    @property
    def action_color(self):
        return {
            'created':  '#059669',
            'edited':   '#6b7280',
            'status':   '#2563eb',
            'assigned': '#d97706',
            'hearing':  '#dc2626',
            'hearing_record': '#1d4ed8',
            'court_order': '#b91c1c',
            'document': '#0891b2',
            'update':   '#7c3aed',
            'task':     '#059669',
            'deadline': '#d97706',
            'outcome':  '#059669',
            'note':     '#d97706',
        }.get(self.action, '#9ca3af')

    @classmethod
    def log(cls, case, action, changed_by=None,
            field_name='', old_value='', new_value='', description=''):
        """Convenience method to create a history entry."""
        return cls.objects.create(
            case=case, action=action, changed_by=changed_by,
            field_name=field_name,
            old_value=str(old_value) if old_value is not None else '',
            new_value=str(new_value) if new_value is not None else '',
            description=description,
        )


# ══════════════════════════════════════════════════════════════════════
# HEARING RECORDS — Manual table of hearing history per case
# ══════════════════════════════════════════════════════════════════════

class HearingRecord(models.Model):
    """
    Manual hearing log table shown in case detail.
    Replaces the description block. Each row = one hearing date.
    """
    case              = models.ForeignKey(Case, on_delete=models.CASCADE,
                                          related_name='hearing_records')
    hearing_date      = models.DateField(help_text='Date this hearing took place or is scheduled')
    next_hearing_date = models.DateField(null=True, blank=True)
    purpose           = models.CharField(max_length=300,
                        help_text='Purpose / agenda of this hearing')
    judge_name        = models.CharField(
        max_length=150, blank=True,
        help_text='Judge or bench before whom the matter was listed',
    )
    outcome_notes     = models.TextField(blank=True,
                        help_text='What happened / result of this hearing')
    scrape_url        = models.URLField(blank=True,
                        help_text='eCourt or court website URL to auto-fetch hearing data')
    scraped_data      = models.TextField(blank=True,
                        help_text='Raw text scraped from court website')
    created_by        = models.ForeignKey(settings.AUTH_USER_MODEL,
                                          on_delete=models.SET_NULL, null=True)
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-hearing_date']
        verbose_name = 'Hearing Record'

    def __str__(self):
        return f"{self.case.case_number} — {self.hearing_date}"


# ══════════════════════════════════════════════════════════════════════
# COURT ORDER PDF LINKS — High Court–style order register (URL → PDF)
# ══════════════════════════════════════════════════════════════════════

class CaseCourtOrderLink(models.Model):
    """
    Register of published orders: each row is a PDF URL (eCourt / court site),
    order date, optional judge, and link label (e.g. case no. / order no. shown in UI).
    """
    case = models.ForeignKey(
        Case, on_delete=models.CASCADE, related_name='court_order_links',
    )
    link_label = models.CharField(
        max_length=200, blank=True,
        help_text='Text shown as the link (e.g. W.P.(C)-IPD 31/2025). Leave blank to use this case’s case number.',
    )
    pdf_url = models.URLField(
        max_length=1000,
        help_text='Full URL to the order PDF (opens in a new tab)',
    )
    order_date = models.DateField(help_text='Date of the order')
    judge_name = models.CharField(
        max_length=200, blank=True,
        help_text='Judge or bench (optional)',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name='court_order_links_added',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-order_date', '-pk']
        verbose_name = 'Court order PDF link'
        verbose_name_plural = 'Court order PDF links'

    def __str__(self):
        return f'{self.case.case_number} — {self.order_date}'

    def link_display_text(self):
        t = (self.link_label or '').strip()
        return t if t else self.case.case_number

    def sort_label_key(self):
        """Safe single-line key for client-side table sorting (HTML data attribute)."""
        return re.sub(r'["<>&]', '', self.link_display_text().lower())


# ══════════════════════════════════════════════════════════════════════
# ALERT OPT-OUT — Stop 7-day reminder, receive only final day alert
# ══════════════════════════════════════════════════════════════════════

class HearingAlertOptOut(models.Model):
    """
    When a recipient clicks 'Stop Alerts' in the 7-day reminder email,
    they fill a form. We store their reason and skip further 7-day alerts.
    They still receive the final 1-day-before alert.
    """
    REASON_CHOICES = [
        ('preparing',    'Already preparing for the hearing'),
        ('acknowledged', 'Already acknowledged / aware'),
        ('attending',    'Confirmed attendance'),
        ('not_relevant', 'Not relevant to me'),
        ('other',        'Other'),
    ]

    case        = models.ForeignKey(Case, on_delete=models.CASCADE,
                                    related_name='alert_opt_outs')
    email       = models.EmailField(help_text='Email address that opted out')
    token       = models.CharField(max_length=64, unique=True,
                  help_text='Secure token embedded in the email link')
    reason      = models.CharField(max_length=20, choices=REASON_CHOICES,
                                   blank=True)
    custom_note = models.TextField(blank=True,
                  help_text='Optional free-text reason')
    hearing_date = models.DateField(help_text='Which hearing date this opt-out is for')
    opted_out_at = models.DateTimeField(null=True, blank=True)
    is_active    = models.BooleanField(default=True,
                   help_text='True = stop 7-day reminders, still send 1-day alert')
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('case', 'email', 'hearing_date')
        verbose_name = 'Hearing Alert Opt-Out'

    def __str__(self):
        return f"{self.email} opted out of alerts for {self.case.case_number}"
