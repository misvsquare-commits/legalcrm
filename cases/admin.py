"""
Admin registrations for all LegalCRM models.
Gives the Django admin panel full visibility and management of every object.
"""
from django.contrib import admin
from django.utils.html import format_html
from .models import (
    Case, CaseUpdate, CaseDocument, Comment,
    CaseDeadline, CaseTask, HearingEmailRecipient, GlobalAlertRecipient, Notification,
    CaseOutcome, TimeEntry, ClientCase, CaseHistory,
    HearingRecord, HearingAlertOptOut, CaseCourtOrderLink, OfflineDocument,
)


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display  = ['case_number', 'title', 'court_name', 'status_badge',
                     'assigned_to', 'next_hearing_date', 'created_at']
    list_filter   = ['status', 'case_type', 'court_name']
    search_fields = ['case_number', 'title', 'petitioner', 'respondent', 'project']
    date_hierarchy = 'filing_date'
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = (
        ('Identity', {'fields': ('case_number', 'title', 'project', 'case_type')}),
        ('Court',    {'fields': ('court_name', 'status', 'assigned_to', 'description')}),
        ('Parties',  {'fields': ('petitioner', 'respondent')}),
        ('Dates',    {'fields': ('filing_date', 'last_hearing_date', 'next_hearing_date')}),
        ('Meta',     {'fields': ('created_by', 'created_at', 'updated_at')}),
    )

    def status_badge(self, obj):
        colours = {'in_progress': '#2563eb', 'disposed': '#059669', 'on_hold': '#dc2626'}
        c = colours.get(obj.status, '#6b7280')
        return format_html(
            '<span style="background:{};color:white;padding:2px 8px;border-radius:4px;font-size:11px">{}</span>',
            c, obj.get_status_display()
        )
    status_badge.short_description = 'Status'


@admin.register(CaseUpdate)
class CaseUpdateAdmin(admin.ModelAdmin):
    list_display  = ['case', 'title', 'date', 'created_by']
    list_filter   = ['date']
    search_fields = ['case__case_number', 'title']


@admin.register(CaseDocument)
class CaseDocumentAdmin(admin.ModelAdmin):
    list_display  = ['case', 'document_type', 'title', 'uploaded_by', 'uploaded_at']
    list_filter   = ['document_type', 'uploaded_at']
    search_fields = ['case__case_number', 'title']


@admin.register(OfflineDocument)
class OfflineDocumentAdmin(admin.ModelAdmin):
    list_display = ['title', 'case', 'storage_location', 'uploaded_by', 'created_at']
    list_filter = ['created_at']
    search_fields = ['title', 'case__case_number', 'storage_location', 'notes']


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display  = ['case', 'user', 'created_at']
    list_filter   = ['created_at']
    search_fields = ['case__case_number', 'comment']


@admin.register(CaseDeadline)
class CaseDeadlineAdmin(admin.ModelAdmin):
    list_display  = ['case', 'title', 'due_date', 'priority', 'is_completed']
    list_filter   = ['priority', 'is_completed', 'due_date']
    search_fields = ['case__case_number', 'title']
    date_hierarchy = 'due_date'


@admin.register(CaseTask)
class CaseTaskAdmin(admin.ModelAdmin):
    list_display  = ['case', 'title', 'assigned_to', 'due_date', 'is_completed']
    list_filter   = ['is_completed', 'due_date']
    search_fields = ['case__case_number', 'title', 'assigned_to__username']


@admin.register(HearingEmailRecipient)
class HearingEmailRecipientAdmin(admin.ModelAdmin):
    list_display  = ['case', 'email', 'name', 'is_active', 'added_at']
    list_filter   = ['is_active']
    search_fields = ['case__case_number', 'email', 'name']


@admin.register(GlobalAlertRecipient)
class GlobalAlertRecipientAdmin(admin.ModelAdmin):
    list_display = ['email', 'name', 'is_active', 'added_at']
    list_filter = ['is_active']
    search_fields = ['email', 'name']


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display  = ['user', 'notif_type', 'title', 'is_read', 'created_at']
    list_filter   = ['notif_type', 'is_read', 'created_at']
    search_fields = ['user__username', 'title']
    date_hierarchy = 'created_at'
    actions = ['mark_all_read']

    def mark_all_read(self, request, queryset):
        queryset.update(is_read=True)
        self.message_user(request, f'{queryset.count()} notifications marked as read.')
    mark_all_read.short_description = 'Mark selected as read'


@admin.register(CaseOutcome)
class CaseOutcomeAdmin(admin.ModelAdmin):
    list_display  = ['case', 'result', 'disposed_date', 'judge_name', 'recorded_by']
    list_filter   = ['result', 'disposed_date']
    search_fields = ['case__case_number', 'case__title', 'judge_name', 'opposing_counsel']
    date_hierarchy = 'disposed_date'


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    list_display  = ['case', 'user', 'date', 'hours', 'description', 'is_billable']
    list_filter   = ['is_billable', 'date', 'user']
    search_fields = ['case__case_number', 'user__username', 'description']
    date_hierarchy = 'date'


@admin.register(ClientCase)
class ClientCaseAdmin(admin.ModelAdmin):
    list_display  = ['client', 'case', 'can_view_documents', 'can_view_updates', 'added_at']
    list_filter   = ['can_view_documents', 'can_view_updates']
    search_fields = ['client__username', 'case__case_number']


@admin.register(CaseHistory)
class CaseHistoryAdmin(admin.ModelAdmin):
    list_display   = ['case', 'action', 'field_name', 'changed_by', 'changed_at']
    list_filter    = ['action', 'changed_at']
    search_fields  = ['case__case_number', 'description', 'field_name']
    date_hierarchy = 'changed_at'
    readonly_fields = ['case', 'action', 'field_name', 'old_value',
                       'new_value', 'description', 'changed_by', 'changed_at']
    def has_add_permission(self, request):             return False
    def has_change_permission(self, request, obj=None): return False


@admin.register(CaseCourtOrderLink)
class CaseCourtOrderLinkAdmin(admin.ModelAdmin):
    list_display = ['case', 'order_date', 'link_label', 'judge_name', 'created_by', 'created_at']
    list_filter = ['order_date']
    search_fields = ['case__case_number', 'link_label', 'judge_name', 'pdf_url']
    date_hierarchy = 'order_date'


@admin.register(HearingRecord)
class HearingRecordAdmin(admin.ModelAdmin):
    list_display  = ['case', 'hearing_date', 'next_hearing_date', 'purpose', 'judge_name', 'created_by']
    list_filter   = ['hearing_date']
    search_fields = ['case__case_number', 'purpose', 'judge_name', 'outcome_notes']
    date_hierarchy = 'hearing_date'


@admin.register(HearingAlertOptOut)
class HearingAlertOptOutAdmin(admin.ModelAdmin):
    list_display  = ['email','case','hearing_date','reason','is_active','opted_out_at']
    list_filter   = ['is_active','reason','hearing_date']
    search_fields = ['email','case__case_number']
    readonly_fields = ['token','created_at']
