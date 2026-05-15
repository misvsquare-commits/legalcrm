# ⚖ LegalCRM — Legal Case Management System

A production-grade CRM for legal teams built on **Django 4.2 + Bootstrap 5**.  
SQLite out of the box — MySQL-ready for production.

---

## ⚡ Quick Start

```bash
bash setup.sh          # creates venv, migrates, seeds, asks to run server
```

Or manually:
```bash
python3 -m venv venv && source venv/bin/activate
pip install "Django>=4.2,<5.0"
python manage.py migrate
python manage.py seed_data
python manage.py runserver
```

Open **http://127.0.0.1:8000/**

---

## 🔑 Login Credentials

| Username       | Password     | Role      | Lands on       |
|---------------|-------------|-----------|---------------|
| `admin`       | `admin123`  | Admin     | Dashboard      |
| `adv_sharma`  | `lawyer123` | Lawyer    | Dashboard      |
| `adv_gupta`   | `lawyer123` | Lawyer    | Dashboard      |
| `assistant1`  | `assist123` | Assistant | Dashboard      |
| `client1`     | `client123` | Client    | Client Portal  |

---

## 🗺 URL Map

| URL | Page | Who |
|-----|------|-----|
| `/` | Dashboard | All staff |
| `/dashboard/` | Main dashboard — stats, charts, hearings | All staff |
| `/workload/` | Lawyer workload, stale cases, overdue deadlines | All staff |
| `/analytics/` | Director analytics — win rates, billing, trends | All staff |
| `/cases/` | Case list with sidebar filters + sort | All staff |
| `/cases/add/` | Add new case | All staff |
| `/cases/<id>/` | Case detail — 8 tabs | All staff |
| `/cases/<id>/edit/` | Edit case | All staff |
| `/cases/<id>/outcome/` | Record win/loss/settlement | All staff |
| `/cases/<id>/clients/` | Grant/revoke client portal access | Admin, Lawyer |
| `/cases/<id>/generate-document/` | AI document generator | All staff |
| `/notifications/` | Full notification centre | All staff |
| `/analytics/billing.csv` | Export billing data as CSV | All staff |
| `/client/` | **Client portal** — read-only case view | Client only |
| `/client/cases/<id>/` | Client's individual case view | Client only |
| `/users/` | User management | Admin only |
| `/admin/` | Django admin panel | Admin only |

---

## 📋 Features by Module

### Core Case Management
- Case CRUD with case number, title, court, type, petitioner/respondent, project, dates
- Status: **In Progress / Disposed / On Hold**
- Sortable table (click any column header)
- Sidebar multi-checkbox filters: Status, Project, Court, Case Type, Lawyer, Hearing date range
- **Paste & Fill** — paste any case text, AI extracts fields automatically
- Export filtered cases as CSV
- Pagination (15 per page)

### Case Detail — 8 Tabs
1. **Timeline** — chronological updates with latest highlighted
2. **Documents** — upload/download (PDF, DOC, DOCX, JPG, PNG — 10MB max)
3. **Comments** — internal team notes
4. **Deadlines** — filing/SLA deadlines with priority + urgency colours
5. **Tasks** — action items assigned to team members
6. **Alert Recipients** — stored emails that receive automatic hearing alerts
7. **Time Log** — hours per lawyer per case, billable flag
8. **AI Analysis** — paste a judgment, Claude extracts dates/risk/next steps

### Dashboard
- KPI cards: Total, In Progress, Disposed, On Hold
- Doughnut chart: cases by status
- Bar chart: cases by court
- Upcoming hearings (30 days) with calendar push buttons
- 10 most recent cases with filing date

### Workload Dashboard (`/workload/`)
- Per-lawyer: open cases, hearings this week, overdue deadlines, pending tasks, load bar
- All upcoming hearings (30-day calendar view)
- All overdue deadlines across the firm
- Stale cases (no update in 45+ days)

### Director Analytics (`/analytics/`)
- **Win Rate %** — overall and per-lawyer
- **Settlement Rate %**
- **Outcome breakdown** doughnut — Won / Lost / Settled / Withdrawn
- **Monthly case filings** bar chart
- **Lawyer performance table** — total, won, lost, settled, win rate bar
- **Court performance table** — win rate per court
- **Case age distribution** — open cases by age bucket + donut chart
- **Time logged by lawyer** — total and billable hours
- **Recent outcomes** list
- Filter by: date range, lawyer, case type
- **Export billing CSV** — all time entries for the period

### Notifications
- In-app bell icon — live badge (polls every 60s)
- Dropdown shows latest 8 with type-coloured dots
- Full page at `/notifications/`
- Types: Hearing, Deadline, Task, Overdue, Case Updated, System
- Auto-created when: hearing date changes, deadline added, task assigned, overdue items detected

### Email Alerts
- Store recipient emails on any case (Alert Recipients tab)
- Alerts fire automatically when hearing date is set or changed
- **7-day** and **1-day** reminders via cron job
- HTML email with case summary, ICS attachment link, View Case button
- Manual "Send Alert Now" button for immediate dispatch
- Calendar sync modal: enter ad-hoc emails → get ICS + Google Calendar links

### Calendar Integration
- Download `.ics` file for any hearing (works with Google Calendar, Apple Calendar, Outlook)
- Direct Google Calendar pre-fill link
- Per-hearing calendar buttons on dashboard

### Client Portal (`/client/`)
- Read-only view for clients (role = `client`)
- See only their linked cases — no internal notes, no comments
- View: case status, next hearing date, updates, documents
- Download documents and export hearing to calendar
- Access controlled per case — admin/lawyer grants/revokes
- Email invite sent to client when access is granted

### AI Features (template-based)
- **Core Summary** on every case: generates a structured “Core Summary (AI Generated)” from saved case details, hearing records, and recent updates
- **Document Generator**: select template (Vakalatnama, Application, Affidavit, Legal Notice, Reply) → generates a structured draft using standard templates + case fields
- Outputs can be saved as case updates, copied, or downloaded as `.txt`

---

## 🗄 Database Models

| Model | Description |
|-------|-------------|
| `User` | Custom user with roles: admin, lawyer, assistant, client |
| `Case` | Core case entity — all case fields |
| `CaseUpdate` | Timeline entries per case |
| `CaseDocument` | Uploaded files per case |
| `Comment` | Internal team notes per case |
| `CaseDeadline` | Filing/SLA deadlines with priority |
| `CaseTask` | Action items assigned to team members |
| `HearingEmailRecipient` | Stored emails for automatic hearing alerts |
| `Notification` | In-app notifications per user |
| `CaseOutcome` | Win/loss/settlement record per disposed case |
| `TimeEntry` | Hours logged per case per lawyer |
| `ClientCase` | Grants a client read-only access to a case |

---

## 🔄 Migrations

```
accounts: 0001_initial → 0002_add_client_role
cases:    0001_initial → 0002_alter_case_status → 0003_phase1_features
                       → 0004_phase2_analytics  → 0005_phase3_client_portal
```

No `makemigrations` needed — all migration files are pre-written.

---

## ⏰ Cron Setup (daily reminders)

Add this to crontab (`crontab -e`):
```bash
0 8 * * * /path/to/venv/bin/python /path/to/legal_crm/manage.py send_reminders
```

This runs `send_reminders` every day at 8am, which:
- Sends hearing reminder emails 7 days before and 1 day before each hearing
- Creates in-app notifications for assigned lawyers
- Flags newly overdue deadlines and tasks

---

## 📧 Email Configuration

Development (console output — default):
```python
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
```

Production (Gmail SMTP — edit `settings.py`):
```python
EMAIL_BACKEND    = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST       = 'smtp.gmail.com'
EMAIL_PORT       = 587
EMAIL_USE_TLS    = True
EMAIL_HOST_USER  = 'your@gmail.com'
EMAIL_HOST_PASSWORD = 'your-app-password'   # Use Gmail App Password
```

---

## 🚀 Production Checklist

```python
# settings.py
DEBUG            = False
SECRET_KEY       = os.environ['DJANGO_SECRET_KEY']
ALLOWED_HOSTS    = ['yourdomain.com']
SITE_URL         = 'https://yourdomain.com'

# Static files
pip install whitenoise
MIDDLEWARE = ['whitenoise.middleware.WhiteNoiseMiddleware', ...rest]
python manage.py collectstatic

# WSGI
pip install gunicorn
gunicorn legal_crm.wsgi:application --bind 0.0.0.0:8000 --workers 3
```

---

## 🔐 Role Access Summary

| Feature | Admin | Lawyer | Assistant | Client |
|---------|:-----:|:------:|:---------:|:------:|
| Dashboard | ✅ | ✅ | ✅ | ❌ |
| All cases | ✅ | Own only | ✅ | ❌ |
| Create/Edit case | ✅ | ✅ | ✅ | ❌ |
| Delete case | ✅ | ❌ | ❌ | ❌ |
| Workload dashboard | ✅ | ✅ | ✅ | ❌ |
| Analytics | ✅ | ✅ | ✅ | ❌ |
| Record outcome | ✅ | ✅ | ✅ | ❌ |
| Log time | ✅ | ✅ | ✅ | ❌ |
| AI features | ✅ | ✅ | ✅ | ❌ |
| Grant client access | ✅ | ✅ | ❌ | ❌ |
| Manage users | ✅ | ❌ | ❌ | ❌ |
| Client portal | ❌ | ❌ | ❌ | ✅ |

---

## ❓ Troubleshooting

**`No module named 'django'`**  → `pip install "Django>=4.2,<5.0"`

**`no such table`**  → `python manage.py migrate`

**`TemplateDoesNotExist`**  → confirm `DIRS: [BASE_DIR / 'templates']` in settings

**Password reset:**
```bash
python manage.py shell -c "
from accounts.models import User
u = User.objects.get(username='admin')
u.set_password('newpassword')
u.save(); print('Done')
"
```

**AI features not working:**  
Core Summary and Document Generator are template-based and run without external API keys.  
If you want a true LLM-backed flow later, we can add a backend-only provider (no keys in browser).
