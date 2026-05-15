from django.urls import path
from . import views

urlpatterns = [
    # Core
    path('',                                                        views.dashboard,                name='dashboard'),
    path('dashboard/',                                              views.dashboard,                name='dashboard_alt'),
    path('dashboard/run-alerts-now/',                               views.run_alerts_now,           name='run_alerts_now'),
    path('dashboard/global-alert-recipients/',                      views.manage_global_recipients, name='manage_global_recipients'),
    path('workload/',                                               views.workload_dashboard,       name='workload_dashboard'),
    path('cases/',                                                  views.case_list,                name='case_list'),
    path('cases/add/',                                              views.CaseCreateView.as_view(), name='case_create'),
    path('cases/<int:pk>/',                                         views.case_detail,              name='case_detail'),
    path('cases/<int:pk>/edit/',                                    views.CaseEditView.as_view(),   name='case_edit'),
    path('cases/<int:pk>/delete/',                                  views.case_delete,              name='case_delete'),
    path('cases/<int:pk>/calendar.ics',                             views.export_hearing_ics,       name='export_hearing_ics'),
    path('cases/<int:pk>/sync-calendar/',                           views.sync_calendar_emails,     name='sync_calendar_emails'),

    # Timeline updates
    path('cases/<int:pk>/updates/add/',                             views.add_case_update,          name='add_case_update'),
    path('cases/<int:pk>/updates/<int:update_pk>/details/',         views.update_case_update_details, name='update_case_update_details'),
    path('cases/<int:pk>/updates/<int:update_pk>/delete/',          views.delete_case_update,       name='delete_case_update'),

    # Documents
    path('cases/<int:pk>/documents/upload/',                        views.upload_document,          name='upload_document'),
    path('documents/<int:doc_pk>/download/',                        views.download_document,        name='download_document'),
    path('cases/<int:pk>/documents/<int:doc_pk>/delete/',           views.delete_document,          name='delete_document'),
    path('documents/manage/',                                        views.manage_documents,         name='manage_documents'),
    path('documents/manage/bulk-upload/',                            views.bulk_upload_case_documents, name='bulk_upload_case_documents'),
    path('documents/manage/bulk-actions/',                           views.bulk_case_document_actions, name='bulk_case_document_actions'),
    path('documents/manage/case/<int:case_pk>/zip/',                 views.download_case_documents_zip, name='download_case_documents_zip'),
    path('documents/manage/case/<int:doc_pk>/delete/',               views.delete_case_document_from_manager, name='delete_case_document_from_manager'),
    path('documents/manage/offline/add/',                            views.add_offline_document,     name='add_offline_document'),
    path('documents/manage/offline/import-csv/',                     views.import_offline_documents_csv, name='import_offline_documents_csv'),
    path('documents/manage/offline/<int:offline_pk>/download/',      views.download_offline_document, name='download_offline_document'),
    path('documents/manage/offline/<int:offline_pk>/delete/',        views.delete_offline_document,  name='delete_offline_document'),

    # Comments
    path('cases/<int:pk>/comments/<int:comment_pk>/delete/',        views.delete_comment,           name='delete_comment'),

    # Deadlines
    path('cases/<int:pk>/deadlines/add/',                           views.add_deadline,             name='add_deadline'),
    path('cases/<int:pk>/deadlines/<int:dl_pk>/complete/',          views.complete_deadline,        name='complete_deadline'),
    path('cases/<int:pk>/deadlines/<int:dl_pk>/delete/',            views.delete_deadline,          name='delete_deadline'),

    # Tasks
    path('cases/<int:pk>/tasks/add/',                               views.add_task,                 name='add_task'),
    path('cases/<int:pk>/tasks/<int:task_pk>/complete/',            views.complete_task,            name='complete_task'),
    path('cases/<int:pk>/tasks/<int:task_pk>/delete/',              views.delete_task,              name='delete_task'),

    # Email recipients
    path('cases/<int:pk>/recipients/',                              views.manage_recipients,        name='manage_recipients'),

    # Notifications
    path('notifications/',                                          views.notification_list,        name='notification_list'),
    path('notifications/count/',                                    views.notification_count_api,   name='notification_count'),
    path('notifications/<int:notif_pk>/read/',                      views.mark_notification_read,   name='mark_notif_read'),
    path('notifications/read-all/',                                 views.mark_all_notifications_read, name='mark_all_notifs_read'),

    # Phase 2 — Analytics
    path('analytics/',                                              views.analytics_dashboard,  name='analytics_dashboard'),
    path('analytics/billing.csv',                                   views.export_billing_csv,   name='export_billing_csv'),
    path('cases/<int:pk>/outcome/',                                 views.record_outcome,        name='record_outcome'),
    path('cases/<int:pk>/time/add/',                                views.add_time_entry,        name='add_time_entry'),
    path('cases/<int:pk>/time/<int:entry_pk>/delete/',              views.delete_time_entry,     name='delete_time_entry'),

    # Phase 3 — Client Portal
    path('client/',                                                 views.client_portal,          name='client_portal'),
    path('client/cases/<int:pk>/',                                  views.client_case_view,       name='client_case_view'),
    path('cases/<int:pk>/clients/',                                 views.manage_client_access,   name='manage_client_access'),
    # Phase 3 — AI Summary
    path('cases/<int:pk>/ai-summary/',                              views.ai_case_summary,        name='ai_case_summary'),
    path('cases/<int:pk>/ai-summary/save/',                         views.save_ai_summary,        name='save_ai_summary'),
    path('cases/<int:pk>/core-summary/',                            views.generate_core_summary,  name='generate_core_summary'),
    path('cases/<int:pk>/gemini/core-summary/',                       views.gemini_core_summary,    name='gemini_core_summary'),
    path('cases/<int:pk>/gemini/hearing-assist/',                   views.gemini_hearing_assist,  name='gemini_hearing_assist'),
    path('cases/<int:pk>/gemini/document-draft/',                     views.gemini_document_draft,  name='gemini_document_draft'),
    # Phase 3 — Document Generator
    path('cases/<int:pk>/generate-document/',                       views.generate_document,      name='generate_document'),
    path('web-scrapping/',                                          views.web_scrapping,          name='web_scrapping'),

    path('cases/<int:pk>/hearings/add/', views.add_hearing_record, name='add_hearing_record'),
    path('cases/<int:pk>/court-orders/add/', views.add_court_order_link, name='add_court_order_link'),
    path('cases/<int:pk>/court-orders/import-from-url/', views.import_court_orders_from_url, name='import_court_orders_from_url'),
    path('cases/<int:pk>/court-orders/import-from-upload/', views.import_court_order_from_upload, name='import_court_order_from_upload'),
    path('cases/<int:pk>/court-orders/<int:link_pk>/delete/', views.delete_court_order_link, name='delete_court_order_link'),
    path('cases/<int:pk>/hearings/<int:rec_pk>/delete/', views.delete_hearing_record, name='delete_hearing_record'),
    path('cases/<int:pk>/hearings/<int:rec_pk>/scrape/', views.scrape_hearing_record, name='scrape_hearing_record'),
    path('cases/<int:pk>/timeline/extract-pdf/', views.extract_pdf_to_timeline, name='extract_pdf_to_timeline'),
    path('alerts/stop/<str:token>/', views.alert_stop_view, name='alert_stop'),
    path('legi/chat/', views.legi_chat, name='legi_chat'),
]
