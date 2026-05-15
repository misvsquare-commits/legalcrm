"""
Cases app forms
"""
from django import forms
from django.core.exceptions import ValidationError
from .models import Case, CaseUpdate, CaseDocument, Comment, OfflineDocument


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = [single_file_clean(data, initial)]
        return result


class CaseForm(forms.ModelForm):
    # Change 1: email recipients for calendar sync
    calendar_emails = forms.CharField(
        required=False,
        label='Sync Calendar To',
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. lawyer@firm.com, client@example.com (comma-separated)',
            'id': 'id_calendar_emails',
        }),
        help_text='Enter email addresses to send calendar invites when the hearing date is set.'
    )

    class Meta:
        model = Case
        fields = [
            'case_number', 'title', 'project', 'court_name', 'case_type',
            'filing_date', 'last_hearing_date', 'next_hearing_date',
            'status', 'assigned_to', 'petitioner', 'respondent', 'description'
        ]
        widgets = {
            'case_number':       forms.TextInput(attrs={'class':'form-control','placeholder':'e.g., HC/2024/001'}),
            'title':             forms.TextInput(attrs={'class':'form-control','placeholder':'Case title'}),
            'project':           forms.TextInput(attrs={'class':'form-control','placeholder':'e.g., Land Acquisition Phase II'}),
            'court_name':        forms.TextInput(attrs={'class':'form-control','placeholder':'e.g., High Court of Delhi'}),
            'case_type':         forms.Select(attrs={'class':'form-select'}),
            'filing_date':       forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'last_hearing_date': forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'next_hearing_date': forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'status':            forms.Select(attrs={'class':'form-select'}),
            'assigned_to':       forms.Select(attrs={'class':'form-select'}),
            'petitioner':        forms.TextInput(attrs={'class':'form-control','placeholder':'Petitioner / Plaintiff name'}),
            'respondent':        forms.TextInput(attrs={'class':'form-control','placeholder':'Respondent / Defendant name'}),
            'description':       forms.Textarea(attrs={'class':'form-control','rows':4,'placeholder':'Case description and notes...'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import User
        self.fields['assigned_to'].queryset = User.objects.filter(role__in=['lawyer','admin'])
        for f in ['project','next_hearing_date','last_hearing_date','description','petitioner','respondent','calendar_emails']:
            self.fields[f].required = False

    def clean_calendar_emails(self):
        raw = self.cleaned_data.get('calendar_emails', '')
        if not raw.strip():
            return []
        emails = [e.strip() for e in raw.replace(';',',').split(',') if e.strip()]
        import re
        pattern = re.compile(r'^[\w\.\+\-]+@[\w\-]+\.[\w\.]{2,}$')
        bad = [e for e in emails if not pattern.match(e)]
        if bad:
            raise forms.ValidationError(f'Invalid email(s): {", ".join(bad)}')
        return emails


class CaseUpdateForm(forms.ModelForm):
    related_document = forms.ModelChoiceField(
        queryset=CaseDocument.objects.none(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = CaseUpdate
        fields = [
            'date', 'title', 'description', 'order_details',
            'filing_response', 'response_received', 'internal_notes',
            'related_document', 'next_action_date'
        ]
        widgets = {
            'date':             forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'title':            forms.TextInput(attrs={'class':'form-control','placeholder':'Update title'}),
            'description':      forms.Textarea(attrs={'class':'form-control','rows':4,'placeholder':'Describe what happened...'}),
            'order_details':    forms.Textarea(attrs={'class':'form-control','rows':2,'placeholder':'Order details / directions'}),
            'filing_response':  forms.Textarea(attrs={'class':'form-control','rows':2,'placeholder':'What we filed / submitted'}),
            'response_received': forms.Textarea(attrs={'class':'form-control','rows':2,'placeholder':'Response received from court/opposite side'}),
            'internal_notes':   forms.Textarea(attrs={'class':'form-control','rows':2,'placeholder':'Internal note (optional)'}),
            'next_action_date': forms.DateInput(attrs={'class':'form-control','type':'date'}),
        }
    def __init__(self, *args, **kwargs):
        case = kwargs.pop('case', None)
        super().__init__(*args, **kwargs)
        self.fields['next_action_date'].required = False
        for f in ['order_details', 'filing_response', 'response_received', 'internal_notes']:
            self.fields[f].required = False
        if case is not None:
            self.fields['related_document'].queryset = case.documents.all().order_by('-uploaded_at')


class CaseDocumentForm(forms.ModelForm):
    class Meta:
        model = CaseDocument
        fields = ['title', 'document_type', 'folder', 'tags', 'file', 'notes']
        widgets = {
            'title':         forms.TextInput(attrs={'class':'form-control','placeholder':'Document title (optional)'}),
            'document_type': forms.Select(attrs={'class':'form-select'}),
            'folder':        forms.TextInput(attrs={'class':'form-control','placeholder':'Folder e.g. Orders / Pleadings'}),
            'tags':          forms.TextInput(attrs={'class':'form-control','placeholder':'Comma-separated tags'}),
            'file':          forms.FileInput(attrs={'class':'form-control','accept':'.pdf,.doc,.docx,.jpg,.jpeg,.png'}),
            'notes':         forms.Textarea(attrs={'class':'form-control','rows':2,'placeholder':'Notes...'}),
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['title'].required = False
        self.fields['folder'].required = False
        self.fields['tags'].required = False
        self.fields['notes'].required = False

    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file:
            import os
            allowed = ['.pdf','.doc','.docx','.jpg','.jpeg','.png']
            ext = os.path.splitext(file.name)[1].lower()
            if ext not in allowed:
                raise forms.ValidationError(f'Invalid file type. Allowed: {", ".join(allowed)}')
        return file


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ['comment']
        widgets = {'comment': forms.Textarea(attrs={'class':'form-control','rows':3,'placeholder':'Add an internal note or comment...'})}


class CaseFilterForm(forms.Form):
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class':'form-control form-control-sm','placeholder':'Search case no., title, party...'})
    )
    assigned_to = forms.ChoiceField(
        required=False,
        widget=forms.Select(attrs={'class':'form-select form-select-sm'})
    )
    hearing_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class':'form-control form-control-sm','type':'date'})
    )
    hearing_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class':'form-control form-control-sm','type':'date'})
    )
    status = forms.MultipleChoiceField(
        required=False,
        choices=Case.STATUS_CHOICES,
        widget=forms.CheckboxSelectMultiple()
    )
    project = forms.MultipleChoiceField(
        required=False, choices=[],
        widget=forms.CheckboxSelectMultiple()
    )
    court = forms.MultipleChoiceField(
        required=False, choices=[],
        widget=forms.CheckboxSelectMultiple()
    )
    case_type = forms.MultipleChoiceField(
        required=False,
        choices=Case.CASE_TYPE_CHOICES,
        widget=forms.CheckboxSelectMultiple()
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import User
        self.fields['assigned_to'].choices = [('','All Lawyers')] + [
            (u.id, u.get_full_name() or u.username)
            for u in User.objects.filter(role__in=['lawyer','admin'])
        ]
        projects = (Case.objects.exclude(project__isnull=True).exclude(project='')
                    .values_list('project',flat=True).distinct().order_by('project'))
        self.fields['project'].choices = [(p,p) for p in projects]
        courts = Case.objects.values_list('court_name',flat=True).distinct().order_by('court_name')
        self.fields['court'].choices = [(c,c) for c in courts]


# ── Phase 1 Forms ─────────────────────────────────────────────────────────

class HearingEmailRecipientForm(forms.ModelForm):
    class Meta:
        from .models import HearingEmailRecipient
        model = HearingEmailRecipient
        fields = ['email', 'name']
        widgets = {
            'email': forms.EmailInput(attrs={'class':'form-control','placeholder':'email@example.com'}),
            'name':  forms.TextInput(attrs={'class':'form-control','placeholder':'Label e.g. Client, Partner (optional)'}),
        }


class CaseDeadlineForm(forms.ModelForm):
    class Meta:
        from .models import CaseDeadline
        model = CaseDeadline
        fields = ['title', 'due_date', 'priority', 'description']
        widgets = {
            'title':       forms.TextInput(attrs={'class':'form-control','placeholder':'e.g. File written submissions'}),
            'due_date':    forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'priority':    forms.Select(attrs={'class':'form-select'}),
            'description': forms.Textarea(attrs={'class':'form-control','rows':2,'placeholder':'Details...'}),
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['description'].required = False


class CaseTaskForm(forms.ModelForm):
    class Meta:
        from .models import CaseTask
        model = CaseTask
        fields = ['title', 'assigned_to', 'due_date', 'description']
        widgets = {
            'title':       forms.TextInput(attrs={'class':'form-control','placeholder':'e.g. Collect affidavit from client'}),
            'assigned_to': forms.Select(attrs={'class':'form-select'}),
            'due_date':    forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'description': forms.Textarea(attrs={'class':'form-control','rows':2,'placeholder':'Details...'}),
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import User
        self.fields['assigned_to'].queryset = User.objects.filter(is_active=True)
        self.fields['assigned_to'].required = False
        self.fields['due_date'].required = False
        self.fields['description'].required = False


# ── Phase 2 Forms ─────────────────────────────────────────────────────────

class CaseOutcomeForm(forms.ModelForm):
    class Meta:
        from .models import CaseOutcome
        model = CaseOutcome
        fields = ['result', 'disposed_date', 'summary', 'judge_name', 'opposing_counsel']
        widgets = {
            'result':           forms.Select(attrs={'class':'form-select'}),
            'disposed_date':    forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'summary':          forms.Textarea(attrs={'class':'form-control','rows':3,
                                    'placeholder':'How was the case resolved? Key points from the judgment...'}),
            'judge_name':       forms.TextInput(attrs={'class':'form-control','placeholder':'Hon. Justice...'}),
            'opposing_counsel': forms.TextInput(attrs={'class':'form-control','placeholder':'Opposing lawyer name'}),
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ['summary','judge_name','opposing_counsel']:
            self.fields[f].required = False


class TimeEntryForm(forms.ModelForm):
    class Meta:
        from .models import TimeEntry
        model = TimeEntry
        fields = ['date', 'hours', 'description', 'is_billable']
        widgets = {
            'date':        forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'hours':       forms.NumberInput(attrs={'class':'form-control','placeholder':'1.5',
                               'min':'0.25','max':'24','step':'0.25'}),
            'description': forms.TextInput(attrs={'class':'form-control',
                               'placeholder':'e.g. Attended hearing, Drafted reply brief'}),
            'is_billable': forms.CheckboxInput(attrs={'class':'form-check-input'}),
        }


class AnalyticsFilterForm(forms.Form):
    """Filter form for the analytics report page."""
    from_date = forms.DateField(required=False,
        widget=forms.DateInput(attrs={'class':'form-control form-control-sm','type':'date'}))
    to_date   = forms.DateField(required=False,
        widget=forms.DateInput(attrs={'class':'form-control form-control-sm','type':'date'}))
    lawyer    = forms.ChoiceField(required=False,
        widget=forms.Select(attrs={'class':'form-select form-select-sm'}))
    case_type = forms.ChoiceField(required=False,
        choices=[('','All Types')] + Case.CASE_TYPE_CHOICES,
        widget=forms.Select(attrs={'class':'form-select form-select-sm'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import User
        self.fields['lawyer'].choices = [('','All Lawyers')] + [
            (u.id, u.get_full_name() or u.username)
            for u in User.objects.filter(role__in=['lawyer','admin'])
        ]


# ── Phase 3 Forms ─────────────────────────────────────────────────────────

class ClientGrantForm(forms.Form):
    """Grant a client user access to a case."""
    client = forms.ChoiceField(
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Client User'
    )
    can_view_documents = forms.BooleanField(required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    can_view_updates   = forms.BooleanField(required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import User
        self.fields['client'].choices = [('', '— Select client —')] + [
            (u.id, f'{u.get_full_name() or u.username} ({u.email})')
            for u in User.objects.filter(role='client', is_active=True)
        ]


class DocumentTemplateForm(forms.Form):
    """Choose document template and override case fields for generation."""
    TEMPLATE_CHOICES = [
        ('vakalatnama',  'Vakalatnama'),
        ('application',  'Application / Petition Draft'),
        ('affidavit',    'Affidavit'),
        ('notice',       'Legal Notice'),
        ('reply',        'Reply / Written Statement'),
    ]
    template_type = forms.ChoiceField(
        choices=TEMPLATE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    additional_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control', 'rows': 3,
            'placeholder': 'Any additional context or special instructions for this document...',
        })
    )


class HearingRecordForm(forms.ModelForm):
    class Meta:
        from .models import HearingRecord
        model = HearingRecord
        fields = [
            'hearing_date', 'next_hearing_date', 'purpose', 'judge_name',
            'outcome_notes', 'scrape_url',
        ]
        widgets = {
            'hearing_date':      forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'next_hearing_date': forms.DateInput(attrs={'class':'form-control','type':'date'}),
            'purpose':           forms.TextInput(attrs={'class':'form-control',
                                     'placeholder':'e.g. Arguments on interim stay, Cross-examination'}),
            'judge_name':        forms.TextInput(attrs={'class':'form-control',
                                     'placeholder':'Hon. Justice… or bench name'}),
            'outcome_notes':     forms.Textarea(attrs={'class':'form-control','rows':3,
                                     'placeholder':'What happened at this hearing...'}),
            'scrape_url':        forms.URLInput(attrs={'class':'form-control',
                                     'placeholder':'https://ecourts.gov.in/... (optional, auto-fetch data)'}),
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ['next_hearing_date', 'judge_name', 'outcome_notes', 'scrape_url']:
            self.fields[f].required = False


class CaseCourtOrderLinkForm(forms.ModelForm):
    """Add a High Court–style order row: PDF URL, order date, optional judge & link label."""

    class Meta:
        from .models import CaseCourtOrderLink
        model = CaseCourtOrderLink
        fields = ['link_label', 'pdf_url', 'order_date', 'judge_name']
        widgets = {
            'link_label': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. W.P.(C)-IPD 31/2025 (shown as link text; optional)',
            }),
            'pdf_url': forms.URLInput(attrs={
                'class': 'form-control',
                'placeholder': 'https://… (direct link to the order PDF)',
            }),
            'order_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'judge_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Hon. Justice… (optional)',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['link_label'].required = False
        self.fields['judge_name'].required = False


class AlertOptOutForm(forms.Form):
    REASON_CHOICES = [
        ('preparing',    'Already preparing for the hearing'),
        ('acknowledged', 'Acknowledged and aware of the date'),
        ('attending',    'Confirmed attendance'),
        ('not_relevant', 'Not relevant to me'),
        ('other',        'Other reason'),
    ]
    reason      = forms.ChoiceField(choices=REASON_CHOICES,
                    widget=forms.RadioSelect(attrs={'class':'form-check-input'}))
    custom_note = forms.CharField(required=False,
                    widget=forms.Textarea(attrs={'class':'form-control','rows':2,
                        'placeholder':'Optional: add a note...'}))

    def clean(self):
        cleaned = super().clean()
        reason = cleaned.get('reason')
        custom_note = (cleaned.get('custom_note') or '').strip()
        if reason == 'other' and not custom_note:
            self.add_error('custom_note', 'Please add a short note when selecting "Other reason".')
        return cleaned


class BulkCaseDocumentUploadForm(forms.Form):
    case = forms.ModelChoiceField(
        queryset=Case.objects.none(),
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    document_type = forms.ChoiceField(
        choices=CaseDocument.DOCUMENT_TYPE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    title_prefix = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Optional title prefix'}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Optional note for all files'}),
    )
    existing_folder = forms.ChoiceField(
        required=False,
        choices=[('', 'Select existing folder (optional)')],
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    custom_folder = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Or create new folder e.g. Pleadings'}),
    )
    tags = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Comma-separated tags'}),
    )
    files = MultipleFileField(
        widget=MultipleFileInput(attrs={'class': 'form-control'}),
    )

    def __init__(self, *args, **kwargs):
        case_queryset = kwargs.pop('case_queryset', Case.objects.none())
        super().__init__(*args, **kwargs)
        self.fields['case'].queryset = case_queryset

        selected_case = None
        if self.is_bound:
            selected_case = self.data.get('case')
        elif self.initial.get('case'):
            selected_case = self.initial.get('case')

        if selected_case:
            try:
                case_id = int(selected_case)
                folders = (
                    CaseDocument.objects.filter(case_id=case_id)
                    .exclude(folder__isnull=True)
                    .exclude(folder__exact='')
                    .values_list('folder', flat=True)
                    .distinct()
                    .order_by('folder')
                )
                self.fields['existing_folder'].choices = [('', 'Select existing folder (optional)')] + [(f, f) for f in folders]
            except (TypeError, ValueError):
                pass

    def clean(self):
        cleaned = super().clean()
        existing_folder = (cleaned.get('existing_folder') or '').strip()
        custom_folder = (cleaned.get('custom_folder') or '').strip()
        if not existing_folder and not custom_folder:
            raise ValidationError('Please choose an existing folder or enter a new folder name.')
        cleaned['resolved_folder'] = custom_folder if custom_folder else existing_folder
        return cleaned


class OfflineDocumentForm(forms.ModelForm):
    class Meta:
        model = OfflineDocument
        fields = ['case', 'title', 'folder', 'tags', 'storage_location', 'file', 'notes']
        widgets = {
            'case': forms.Select(attrs={'class': 'form-select'}),
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Document title'}),
            'folder': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Optional folder'}),
            'tags': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'tag1, tag2'}),
            'storage_location': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Physical location e.g. Rack A / Box 4',
            }),
            'file': forms.FileInput(attrs={'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Optional note'}),
        }

    def __init__(self, *args, **kwargs):
        case_queryset = kwargs.pop('case_queryset', Case.objects.none())
        super().__init__(*args, **kwargs)
        self.fields['case'].queryset = case_queryset
        self.fields['case'].required = False
        self.fields['folder'].required = False
        self.fields['tags'].required = False
        self.fields['storage_location'].required = False
        self.fields['notes'].required = False
