"""
Cases app views — Dashboard, Case CRUD, Updates, Documents, Comments, CSV, ICS
"""
import csv
import io
import os
import uuid
import json
import re
import zipfile
from collections import OrderedDict
from datetime import datetime
import urllib.request
import urllib.error
import ssl
from html import unescape
from urllib.parse import urljoin, urlparse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils.decorators import method_decorator
from django.views import View, generic
from django.core.paginator import Paginator
from django.core.files.base import ContentFile
from django.db.models import Q, Count
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.core.cache import cache

from .models import (
    Case, CaseUpdate, CaseDocument, OfflineDocument, Comment,
    CaseUpdateDocumentAttachment,
)
from .forms import (
    CaseForm, CaseUpdateForm, CaseDocumentForm,
    CommentForm, CaseFilterForm, CaseCourtOrderLinkForm,
    BulkCaseDocumentUploadForm, OfflineDocumentForm,
)
from . import gemini_service
from .signals import (
    log_court_order_link_event,
    log_deadline_event,
    log_document_event,
    log_hearing_record_event,
    log_outcome_event,
    log_task_event,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _base_queryset(user):
    if user.role == 'lawyer':
        return Case.objects.filter(assigned_to=user)
    return Case.objects.all()


def _is_order_timeline_update(update_obj):
    """Heuristic: timeline row created from order/PDF processing."""
    title = (getattr(update_obj, 'title', '') or '').strip().lower()
    desc = (getattr(update_obj, 'description', '') or '').strip().lower()
    return (
        title.startswith('coram:')
        or title.startswith('[pdf timeline]')
        or 'source pdf:' in desc
    )


def _sync_case_hearing_dates_from_activity(case):
    """
    Add-on sync only:
    - last_hearing_date from latest order/timeline/hearing activity
    - next_hearing_date from latest next-action/next-listing dates
    Keeps existing behavior; updates only when derived values are available.
    """
    latest_order_update = (
        case.updates.all()
        .order_by('-date', '-created_at')
        .first()
    )
    latest_hearing = (
        case.hearing_records.all()
        .order_by('-hearing_date', '-created_at')
        .first()
    )
    latest_order_row = (
        case.court_order_links.all()
        .order_by('-order_date', '-pk')
        .first()
    )

    derived_last = None
    derived_next = None

    if latest_order_update and _is_order_timeline_update(latest_order_update):
        derived_last = latest_order_update.date
        if latest_order_update.next_action_date:
            derived_next = latest_order_update.next_action_date

    if not derived_last and latest_hearing and latest_hearing.hearing_date:
        derived_last = latest_hearing.hearing_date
    if not derived_next and latest_hearing and latest_hearing.next_hearing_date:
        derived_next = latest_hearing.next_hearing_date

    if not derived_last and latest_order_row and latest_order_row.order_date:
        derived_last = latest_order_row.order_date

    latest_any_update = case.updates.all().order_by('-date', '-created_at').first()
    if not derived_next and latest_any_update and latest_any_update.next_action_date:
        derived_next = latest_any_update.next_action_date

    to_update = []
    if derived_last and case.last_hearing_date != derived_last:
        case.last_hearing_date = derived_last
        to_update.append('last_hearing_date')
    if derived_next and case.next_hearing_date != derived_next:
        case.next_hearing_date = derived_next
        to_update.append('next_hearing_date')
    if to_update:
        case.save(update_fields=to_update)

def _legi_aggregate_snapshot(user):
    """Counts and recent cases for Legi / Gemini CRM context."""
    from .models import CaseDeadline, CaseTask, Notification

    today = timezone.now().date()
    week_end = today + timezone.timedelta(days=7)
    month_end = today + timezone.timedelta(days=30)
    qs = _base_queryset(user)
    upcoming = (
        qs.filter(next_hearing_date__gte=today, next_hearing_date__lte=week_end)
        .exclude(status='disposed')
        .count()
    )
    return {
        'total': qs.count(),
        'in_progress': qs.filter(status='in_progress').count(),
        'disposed': qs.filter(status='disposed').count(),
        'on_hold': qs.filter(status='on_hold').count(),
        'overdue_deadlines': CaseDeadline.objects.filter(
            due_date__lt=today, is_completed=False
        ).count(),
        'pending_tasks': CaseTask.objects.filter(is_completed=False).count(),
        'unread_notifications': Notification.objects.filter(
            user=user, is_read=False
        ).count(),
        'hearings_month': qs.filter(
            next_hearing_date__gte=today, next_hearing_date__lte=month_end
        ).exclude(status='disposed').count(),
        'upcoming_week': upcoming,
        'recent_cases': qs.select_related('assigned_to').order_by('-updated_at')[:12],
    }


def _legi_crm_context_block(user):
    s = _legi_aggregate_snapshot(user)
    lines = [
        f"User: {user.get_full_name() or user.username} (role: {getattr(user, 'role', '')})",
        f"Total cases (visible scope): {s['total']}",
        f"In progress: {s['in_progress']} | Disposed: {s['disposed']} | On hold: {s['on_hold']}",
        f"Hearings in next 7 days: {s['upcoming_week']} | Next 30 days: {s['hearings_month']}",
        f"Overdue deadlines (visible cases): {s['overdue_deadlines']} | Pending tasks: {s['pending_tasks']}",
        f"Unread notifications for this user: {s['unread_notifications']}",
        '',
        'Recent / active cases (do not invent beyond this list):',
    ]
    for c in s['recent_cases']:
        assign = c.assigned_to.get_full_name() if c.assigned_to else 'Unassigned'
        lines.append(
            f"- {c.case_number} | {c.title[:120]} | {c.get_status_display()} | "
            f"Next hearing: {_fmt_date(c.next_hearing_date)} | Lawyer: {assign}"
        )
    return '\n'.join(lines)


def _legi_gemini_reply(user, question: str) -> str:
    sys_inst = (
        "You are Legi, an assistant inside the LegalCRM web app used by a law firm. "
        "You help staff interpret workload data, suggest CRM navigation ideas, and draft neutral checklists. "
        "You are not a lawyer: do not give legal advice, predictions of outcomes, or cite law unless the user "
        "pastes authoritative text. Use only the CRM snapshot plus the user's words; if data is missing, say so. "
        "Prefer concise sections and bullet lists. Plain text only (no HTML)."
    )
    return gemini_service.generate_text(
        system_instruction=sys_inst,
        user_prompt=f"--- CRM snapshot ---\n{_legi_crm_context_block(user)}\n\n--- User message ---\n{question}",
        max_output_tokens=4096,
        temperature=0.35,
    )


def _fmt_date(d):
    if not d:
        return '—'
    try:
        return d.strftime('%d %b %Y')
    except Exception:
        return str(d)


def _first_nonempty(*vals, default='—'):
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default


def _extract_bullets(text, limit=3):
    """
    Heuristic bullet extraction from a free-text description.
    Keeps it deterministic (no external AI) while still useful.
    """
    if not text:
        return []
    raw = [ln.strip(' \t-•\u2022') for ln in str(text).splitlines()]
    items = [ln for ln in raw if ln and len(ln) >= 6]
    out = []
    for it in items:
        if it.lower().startswith(('facts:', 'issues:', 'timeline:', 'summary:')):
            continue
        out.append(it[:180])
        if len(out) >= limit:
            break
    return out


def _build_core_summary(case):
    """
    Builds a generated narrative summary using case data + recent records.
    The user's earlier structure is treated as guidance, not a rigid template.
    """
    latest_update = case.updates.all().order_by('-date', '-created_at').first()
    latest_hearing = case.hearing_records.all().order_by('-hearing_date', '-created_at').first()

    # Procedural timeline (auto extracted)
    timeline = []
    if case.filing_date:
        timeline.append((case.filing_date, 'Filing'))
    if case.last_hearing_date:
        timeline.append((case.last_hearing_date, 'Last hearing'))
    if case.next_hearing_date:
        timeline.append((case.next_hearing_date, 'Next hearing scheduled'))

    # Add a few latest updates
    for u in case.updates.all().order_by('-date', '-created_at')[:3]:
        timeline.append((u.date, f'Update: {u.title}'))

    # Add a few hearing records
    for r in case.hearing_records.all().order_by('-hearing_date', '-created_at')[:3]:
        timeline.append((r.hearing_date, f'Hearing: {r.purpose}'))

    # De-dup + sort asc
    seen = set()
    uniq = []
    for d, ev in timeline:
        key = (str(d), ev)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((d, ev))
    uniq.sort(key=lambda x: (x[0] or timezone.now().date()))

    # Key issues / facts (heuristic from description + last update)
    issues = _extract_bullets(case.description, limit=3)
    facts = _extract_bullets(_first_nonempty(getattr(latest_update, 'description', ''), ''), limit=3)
    if not facts:
        facts = _extract_bullets(case.description, limit=3)

    # Latest order insight (best-effort from latest update / hearing record)
    order_date = None
    order_text = ''
    if latest_update and ('order' in latest_update.title.lower() or 'order' in latest_update.description.lower()):
        order_date = latest_update.date
        order_text = latest_update.description
    elif latest_hearing and latest_hearing.outcome_notes:
        order_date = latest_hearing.hearing_date
        order_text = latest_hearing.outcome_notes
    else:
        order_date = latest_update.date if latest_update else None
        order_text = latest_update.description if latest_update else ''

    order_lines = _extract_bullets(order_text, limit=3)
    if order_lines:
        order_summary = ' '.join(order_lines)[:420]
    else:
        order_summary = (_first_nonempty(order_text, default='—')[:420]).strip() or '—'

    # Insights (optional)
    stage = 'Admission' if case.status == 'in_progress' else ('Final Hearing' if case.status == 'disposed' else 'Notice')
    progress = 'Early'
    if case.last_hearing_date and case.filing_date:
        days = (case.last_hearing_date - case.filing_date).days
        if days > 365:
            progress = 'Advanced'
        elif days > 120:
            progress = 'Mid'
    risk = 'Medium'
    if case.status == 'disposed':
        risk = 'Low'
    elif case.overdue_deadlines_count > 0:
        risk = 'High'

    summary_paragraph = (
        f"This case titled '{case.title}' (Case No: {case.case_number}) is pending before {case.court_name}. "
        f"It has been filed by {(_first_nonempty(case.petitioner, default='the petitioner'))} against "
        f"{(_first_nonempty(case.respondent, default='the respondent'))}. "
        f"The matter concerns {(_first_nonempty(case.description, default='the dispute/issues recorded in the case file')[:160]).rstrip('.')}. "
        f"The case is currently marked as {case.get_status_display()}, and the next scheduled hearing is {_fmt_date(case.next_hearing_date)}."
    )

    def bullet_block(items, fallback):
        if items:
            return '\n'.join([f"- {i}" for i in items])
        return f"- {fallback}"

    # Generated (non-rigid) wording for better readability
    filing_party = _first_nonempty(case.petitioner, default='the petitioner')
    opposing_party = _first_nonempty(case.respondent, default='the respondent')
    requested_relief = _first_nonempty(
        getattr(latest_update, 'title', ''),
        case.description,
        default='reliefs and directions as sought in pleadings'
    )

    generated_summary = (
        f"The matter concerns {case.title} bearing case number {case.case_number}, pending before {case.court_name}. "
        f"It is filed by {filing_party} against {opposing_party}. "
        f"From the available records, the dispute appears to relate to {(_first_nonempty(case.description, default='the issues captured in the case file')[:190]).rstrip('.')}. "
        f"The principal request/challenge presently reflected in the file is {requested_relief[:150]}. "
        f"Procedurally, the case is currently marked as {case.get_status_display()} with next listed hearing on {_fmt_date(case.next_hearing_date)}."
    )

    out = []
    out.append("Core Summary (AI Generated)")
    out.append("")
    out.append("Generated Summary")
    out.append(generated_summary)
    out.append("")
    out.append("Key Issues Identified")
    out.append(bullet_block(issues, "Key issues are not clearly recorded; add concise issue points in case updates for stronger summaries."))
    out.append("")
    out.append("Important Facts")
    out.append(bullet_block(facts, "Important facts are limited in current notes; add factual chronology in case updates."))
    out.append("")
    out.append("Procedural Timeline (Auto Extracted)")
    if uniq:
        for d, ev in uniq[:10]:
            out.append(f"{_fmt_date(d)} - {ev}")
    else:
        out.append("No timeline events available.")
    out.append("")
    out.append("Latest Order Insight")
    out.append(f"Order Date: {_fmt_date(order_date)}")
    out.append(f"Order Summary: {order_summary if order_summary else 'No latest order notes available.'}")
    out.append("")
    out.append("AI Insights")
    out.append(f"Case Stage: {stage}")
    out.append(f"Estimated Progress: {progress}")
    out.append(f"Risk/Impact Level: {risk}")
    out.append(f"Category: {case.get_case_type_display()}")
    return "\n".join(out).strip()


def _apply_filters(cases, form):
    d = form.cleaned_data
    if d.get('search'):
        q = d['search']
        cases = cases.filter(
            Q(case_number__icontains=q)|Q(title__icontains=q)|
            Q(petitioner__icontains=q)|Q(respondent__icontains=q)|
            Q(court_name__icontains=q)|Q(project__icontains=q)
        )
    if d.get('assigned_to'):
        cases = cases.filter(assigned_to_id=d['assigned_to'])
    if d.get('hearing_from'):
        cases = cases.filter(next_hearing_date__gte=d['hearing_from'])
    if d.get('hearing_to'):
        cases = cases.filter(next_hearing_date__lte=d['hearing_to'])
    if d.get('status'):
        cases = cases.filter(status__in=d['status'])
    if d.get('project'):
        cases = cases.filter(project__in=d['project'])
    if d.get('court'):
        cases = cases.filter(court_name__in=d['court'])
    if d.get('case_type'):
        cases = cases.filter(case_type__in=d['case_type'])
    return cases


def _to_bool(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _clean_space(text):
    return re.sub(r'\s+', ' ', (text or '')).strip()


def _build_request_headers(user_agent=None, referer=None):
    headers = {
        'User-Agent': user_agent or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 LegalCRM-Scraper/1.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if referer:
        headers['Referer'] = referer
    return headers


def _fetch_url_bytes(target_url, timeout_sec=20, verify_ssl=True, headers=None):
    req = urllib.request.Request(target_url, headers=headers or _build_request_headers())
    context = None
    if not verify_ssl:
        context = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout_sec, context=context) as resp:
        return {
            'content_type': (resp.headers.get('Content-Type') or '').lower(),
            'bytes': resp.read(),
            'final_url': resp.geturl() or target_url,
        }


def _extract_links_from_html(body, base_url):
    """
    Multi-strategy link extraction for mixed court HTML formats.
    """
    links = []

    # Standard attributes
    for attr in ('href', 'src', 'data', 'data-src', 'data-href'):
        for val in re.findall(rf'{attr}\s*=\s*["\']([^"\']+)["\']', body, flags=re.IGNORECASE):
            links.append(urljoin(base_url, unescape(val)))

    # onclick/window.open/location style
    onclick_patterns = [
        r'window\.open\(\s*["\']([^"\']+)["\']',
        r'location\.href\s*=\s*["\']([^"\']+)["\']',
        r'document\.location\s*=\s*["\']([^"\']+)["\']',
        r'["\'](/[^"\']+\.pdf[^"\']*)["\']',
    ]
    for pat in onclick_patterns:
        for val in re.findall(pat, body, flags=re.IGNORECASE):
            links.append(urljoin(base_url, unescape(val)))

    # Direct PDF-like URLs found in JS/text
    for val in re.findall(r'https?://[^\s"\'<>]+', body, flags=re.IGNORECASE):
        if '.pdf' in val.lower():
            links.append(val)

    # Dedup + basic URL sanity
    out = []
    seen = set()
    for ln in links:
        ln = (ln or '').strip()
        if not ln or ln.startswith('javascript:') or ln.startswith('mailto:'):
            continue
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    return out


def _extract_case_signals(text_blob):
    """
    Best-effort extraction of common court case metadata from free text.
    """
    txt = _clean_space(text_blob)
    case_numbers = re.findall(r'\b[A-Z]{1,8}[./-][A-Z0-9./-]{2,40}\b', txt, flags=re.IGNORECASE)
    dates = re.findall(
        r'\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b',
        txt,
        flags=re.IGNORECASE
    )
    return {
        'case_numbers': list(dict.fromkeys(case_numbers))[:10],
        'dates': list(dict.fromkeys(dates))[:15],
    }


def _format_extracted_text(raw_text, max_chars=12000):
    """Normalize extracted text into a readable paragraph/bullet format."""
    if not raw_text:
        return ''
    lines = [ln.strip() for ln in str(raw_text).splitlines()]
    cleaned = []
    for ln in lines:
        if not ln:
            if cleaned and cleaned[-1] != '':
                cleaned.append('')
            continue
        ln = re.sub(r'\s+', ' ', ln)
        cleaned.append(ln)
    text = '\n'.join(cleaned).strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:max_chars]


def _pdf_reader_factory():
    """Return PdfReader class from pypdf (preferred) or PyPDF2."""
    try:
        from pypdf import PdfReader
        return PdfReader
    except Exception:
        pass
    try:
        from PyPDF2 import PdfReader
        return PdfReader
    except Exception:
        return None


def _extract_pdf_text_from_bytes(pdf_bytes, max_pages=30):
    """Best-effort PDF text extraction without hard dependency lock-in."""
    PdfReader = _pdf_reader_factory()
    if PdfReader is None:
        return '', (
            'PDF text extractor unavailable. Install: pip install pypdf '
            '(or PyPDF2).'
        )

    try:
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            page_txt = page.extract_text() or ''
            if page_txt.strip():
                parts.append(page_txt)
        text = '\n\n'.join(parts).strip()
        if not text:
            return '', (
                'No selectable text found in this PDF (common for scanned/image-only '
                'PDFs). Try an original digital PDF, or use OCR tools outside the app.'
            )
        return text, ''
    except Exception as e:
        return '', f'PDF extraction failed: {e}'


def _looks_like_pdf_bytes(data):
    if not data or len(data) < 5:
        return False
    head = data.lstrip()[:4096]
    return b'%PDF' in head


def _http_fetch_binary(url, *, referer=None, user_agent=None, verify_ssl=True, timeout_sec=45):
    """GET URL and return raw bytes (best-effort)."""
    req = urllib.request.Request(
        url,
        headers=_build_request_headers(user_agent=user_agent, referer=referer),
    )
    ctx = None if verify_ssl else ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout_sec, context=ctx) as resp:
        return resp.read()


def _ocr_image_bytes(image_bytes):
    """OCR for images when pytesseract is available."""
    try:
        import pytesseract
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        return pytesseract.image_to_string(img) or '', ''
    except Exception as e:
        return '', f'OCR unavailable: {e}'


def _extract_timeline_points_from_text(text, limit=8):
    """Parse date-like lines from text into timeline points."""
    if not text:
        return []
    points = []
    for ln in text.splitlines():
        line = ln.strip()
        if len(line) < 8:
            continue
        m = re.search(r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})', line)
        if not m:
            continue
        date_text = m.group(1)
        desc = line.replace(date_text, '').strip(' :-\u2013\u2014')
        if not desc:
            desc = 'Event noted from PDF'
        points.append({'date_text': date_text, 'description': desc[:180]})
        if len(points) >= limit:
            break
    return points


def _extract_judge_name_from_text(text):
    if not text:
        return ''
    patterns = [
        r'HON[\' ]*BLE\s+MR\.?\s+JUSTICE\s+([A-Z][A-Z .]{2,120})',
        r'HON[\' ]*BLE\s+MS\.?\s+JUSTICE\s+([A-Z][A-Z .]{2,120})',
        r'HON[\' ]*BLE\s+MRS\.?\s+JUSTICE\s+([A-Z][A-Z .]{2,120})',
        r'JUSTICE\s+([A-Z][A-Z .]{2,120})',
        r'CORAM[:\s-]+([A-Z][A-Z .,&]{3,160})',
    ]
    upper = str(text).upper()
    for pat in patterns:
        m = re.search(pat, upper, flags=re.IGNORECASE)
        if m:
            name = re.sub(r'\s+', ' ', m.group(1)).strip(' .,:;-')
            if len(name) >= 4:
                return name.title()
    return ''


def _extract_last_textual_date(text):
    """
    Finds the last textual date like 'OCTOBER 14, 2025' in the document.
    Returns (parsed_date|None, original_text|'' ).
    """
    if not text:
        return None, ''
    pattern = r'\b([A-Z]{3,9}\s+\d{1,2},\s+\d{4})\b'
    matches = re.findall(pattern, str(text).upper())
    if not matches:
        return None, ''
    raw = matches[-1].strip()
    for fmt in ('%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(raw, fmt).date(), raw.title()
        except Exception:
            continue
    return None, raw.title()


def _parse_legal_date(raw):
    """Parse common court date formats and return date or None."""
    val = (raw or '').strip()
    if not val:
        return None
    for fmt in (
        '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y',
        '%d/%m/%y', '%d-%m-%y', '%d.%m.%y',
        '%Y-%m-%d',
        '%d %b %Y', '%d %B %Y',
        '%b %d, %Y', '%B %d, %Y',
    ):
        try:
            return datetime.strptime(val, fmt).date()
        except Exception:
            continue
    return None


def _extract_coram_title(text):
    """
    Extract title from CORAM block, e.g.
    CORAM: HON'BLE MR. JUSTICE TEJAS KARIA
    """
    if not text:
        return ''
    compact = re.sub(r'\r', '\n', str(text))
    coram_block = re.search(
        r'CORAM\s*:\s*([\s\S]{0,260})',
        compact,
        flags=re.IGNORECASE
    )
    if not coram_block:
        return ''
    chunk = coram_block.group(1)
    lines = [re.sub(r'\s+', ' ', ln).strip(' :-\n\t') for ln in chunk.splitlines()]
    line = ''
    for ln in lines:
        if not ln:
            continue
        if re.match(r'^(order|judgment)$', ln, flags=re.IGNORECASE):
            break
        line = ln
        break
    if not line:
        return 'CORAM'
    return f'CORAM: {line}'


def _extract_order_date_from_text(text):
    """
    Extract order date with priority:
    1) Date immediately near ORDER heading.
    2) Last textual month date in the document.
    """
    if not text:
        return None, ''
    src = re.sub(r'\r', '\n', str(text))
    compact_order = re.sub(r'O\s*R\s*D\s*E\s*R', 'ORDER', src, flags=re.IGNORECASE)
    m = re.search(
        r'ORDER[\s:%\-\n]{0,80}(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})',
        compact_order,
        flags=re.IGNORECASE,
    )
    if m:
        raw = m.group(1).strip()
        parsed = _parse_legal_date(raw)
        if parsed:
            return parsed, raw
    last_txt_dt, last_txt_raw = _extract_last_textual_date(src)
    return last_txt_dt, last_txt_raw


def _extract_next_listing_date(text):
    """Extract 'list on <date>' date from order text."""
    if not text:
        return None, ''
    src = str(text)
    m = re.search(
        r'\b(?:list(?:ed)?\s+on|list\s+these\s+matters?\s+on)\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})',
        src,
        flags=re.IGNORECASE,
    )
    if not m:
        return None, ''
    raw = m.group(1).strip()
    return _parse_legal_date(raw), raw


def _extract_order_numbered_points(text, limit=8):
    """Extract numbered directions from ORDER body as point-wise text."""
    if not text:
        return []
    src = re.sub(r'\r', '\n', str(text))
    src = re.sub(r'O\s*R\s*D\s*E\s*R', 'ORDER', src, flags=re.IGNORECASE)
    order_start = re.search(r'\bORDER\b', src, flags=re.IGNORECASE)
    body = src[order_start.end():] if order_start else src
    lines = [re.sub(r'\s+', ' ', ln).strip() for ln in body.splitlines()]
    points = []
    current = ''
    for ln in lines:
        if not ln:
            continue
        if re.match(r'^(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\b', ln, flags=re.IGNORECASE):
            continue
        if re.match(r'^\d+\.\s*', ln):
            if current:
                points.append(current.strip())
            current = re.sub(r'^\d+\.\s*', '', ln).strip()
            if len(points) >= limit:
                break
            continue
        if current:
            if re.match(r'^(digitally signed|page \d+|signature not verified)', ln, flags=re.IGNORECASE):
                continue
            current = f'{current} {ln}'.strip()
    if current and len(points) < limit:
        points.append(current.strip())
    out = []
    for idx, p in enumerate(points[:limit], start=1):
        out.append(f'{idx}. {p[:380]}')
    return out


def _extract_key_points_after_coram(text, limit=8):
    """
    Build compact bullet points from text after CORAM section.
    """
    if not text:
        return []
    up = str(text)
    m = re.search(r'CORAM\s*:\s*[\s\S]*?(?:\n\n|\r\n\r\n)', up, flags=re.IGNORECASE)
    body = up[m.end():] if m else up
    lines = [re.sub(r'\s+', ' ', ln).strip() for ln in body.splitlines()]
    out = []
    for ln in lines:
        if not ln:
            continue
        if len(ln) < 22:
            continue
        if re.match(r'^(order|judgment|digitally signed|page \d+)', ln, flags=re.IGNORECASE):
            continue
        if re.match(r'^[A-Z]{3,9}\s+\d{1,2},\s+\d{4}$', ln.strip(), flags=re.IGNORECASE):
            continue
        out.append(ln[:220])
        if len(out) >= limit:
            break
    return out


def _extract_href_from_html_anchor(anchor_html):
    if not anchor_html:
        return ''
    m = re.search(r"""href=['"]([^'"]+)['"]""", str(anchor_html), flags=re.IGNORECASE)
    return unescape(m.group(1)).strip() if m else ''


def _fetch_delhi_high_court_orders(target_url, timeout_sec=30, verify_ssl=True, user_agent=None):
    params = {'draw': '1', 'start': '0', 'length': '100'}
    req = urllib.request.Request(
        f"{target_url}?draw=1&start=0&length=200",
        headers={
            **_build_request_headers(user_agent=user_agent),
            'X-Requested-With': 'XMLHttpRequest',
        }
    )
    ctx = None if verify_ssl else ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout_sec, context=ctx) as resp:
        raw = resp.read().decode('utf-8', errors='ignore')
    payload = json.loads(raw or '{}')
    rows = []
    for item in payload.get('data', []) or []:
        pdf_url = _extract_href_from_html_anchor(item.get('case_no_order_link', ''))
        if not pdf_url:
            continue
        pdf_url = urljoin(target_url, pdf_url)
        order_date = (item.get('order_date') or {}).get('display') or item.get('orddate') or ''
        link_label = _clean_space(item.get('caseno') or '')
        rows.append({
            'pdf_url': pdf_url,
            'order_date': order_date,
            'link_label': link_label,
            'raw': item,
        })
    return rows


def _fetch_generic_listing_history_orders(target_url, timeout_sec=30, verify_ssl=True, user_agent=None):
    """
    Parse generic court listing-history tables where each row has:
    - a listing/order date column
    - a PDF anchor (often "View PDF")
    """
    fetch = _fetch_url_bytes(
        target_url,
        timeout_sec=timeout_sec,
        verify_ssl=verify_ssl,
        headers=_build_request_headers(user_agent=user_agent),
    )
    html = fetch['bytes'].decode('utf-8', errors='ignore')
    base_url = fetch.get('final_url') or target_url

    rows = []
    seen = set()
    tr_blocks = re.findall(r'<tr\b[^>]*>([\s\S]*?)</tr>', html, flags=re.IGNORECASE)
    for tr in tr_blocks:
        link_match = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tr, flags=re.IGNORECASE)
        if not link_match:
            continue
        pdf_url = urljoin(base_url, unescape(link_match.group(1).strip()))
        if '.pdf' not in pdf_url.lower():
            anchor_text = _clean_space(re.sub(r'<[^>]+>', ' ', tr))
            if 'view pdf' not in anchor_text.lower() and 'order' not in anchor_text.lower():
                continue

        date_candidates = re.findall(r'\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b', tr)
        if not date_candidates:
            continue
        order_date = date_candidates[0]

        label = _clean_space(re.sub(r'<[^>]+>', ' ', tr))
        label = re.sub(r'\bview\s*pdf\b', '', label, flags=re.IGNORECASE).strip(' |-:')
        label = (label[:200] if label else '')

        key = (pdf_url, order_date)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            'pdf_url': pdf_url,
            'order_date': order_date,
            'link_label': label,
            'raw': {'source': 'generic_listing_history'},
        })

    return rows


def _extract_order_rows_from_html_snippet(html_text, base_url=''):
    """
    Parse pasted HTML snippet (inspect-copy) and extract order rows.
    Works for table layouts containing date + link columns.
    """
    if not html_text:
        return []
    html = str(html_text)
    rows = []
    seen = set()
    tr_blocks = re.findall(r'<tr\b[^>]*>([\s\S]*?)</tr>', html, flags=re.IGNORECASE)
    for tr in tr_blocks:
        href_match = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tr, flags=re.IGNORECASE)
        if not href_match:
            continue
        href_raw = unescape(href_match.group(1).strip())
        pdf_url = urljoin(base_url, href_raw) if base_url else href_raw
        if '.pdf' not in pdf_url.lower() and 'gen_pdf.php' not in pdf_url.lower():
            continue

        date_candidates = re.findall(r'\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b', tr)
        if not date_candidates:
            continue
        order_date = date_candidates[0]

        row_text = _clean_space(re.sub(r'<[^>]+>', ' ', tr))
        row_text = re.sub(r'\bview\s*pdf\b', '', row_text, flags=re.IGNORECASE).strip(' |-:')
        row_text = row_text[:200] if row_text else ''

        key = (pdf_url, order_date)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            'pdf_url': pdf_url,
            'order_date': order_date,
            'link_label': row_text,
            'raw': {'source': 'pasted_html'},
        })
    return rows


SCRAPE_PROFILES = {
    'auto': {
        'label': 'Auto (detect by domain)',
        'user_agent': '',
        'follow_embeds': True,
        'verify_ssl': True,
        'timeout_sec': 20,
        'pdf_keyword_hints': [],
    },
    'ecourts': {
        'label': 'eCourts',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
        'follow_embeds': True,
        'verify_ssl': True,
        'timeout_sec': 30,
        'pdf_keyword_hints': ['cis', 'order', 'judgment', 'download'],
    },
    'supreme_court': {
        'label': 'Supreme Court',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
        'follow_embeds': True,
        'verify_ssl': True,
        'timeout_sec': 30,
        'pdf_keyword_hints': ['judgment', 'order', 'pdf', 'archive'],
    },
    'high_court': {
        'label': 'High Court (generic)',
        'user_agent': '',
        'follow_embeds': True,
        'verify_ssl': True,
        'timeout_sec': 25,
        'pdf_keyword_hints': ['cause', 'order', 'judgment', 'daily'],
    },
}


def _detect_profile_from_url(target_url):
    host = (urlparse(target_url).hostname or '').lower()
    if 'ecourts' in host or 'services.ecourts' in host:
        return 'ecourts'
    if 'sci.gov.in' in host or 'supremecourt' in host:
        return 'supreme_court'
    if 'highcourt' in host or '.hc' in host:
        return 'high_court'
    return 'auto'


def _effective_profile(profile_key, target_url):
    key = profile_key if profile_key in SCRAPE_PROFILES else 'auto'
    if key == 'auto':
        detected = _detect_profile_from_url(target_url)
        if detected != 'auto':
            return detected, SCRAPE_PROFILES[detected]
    return key, SCRAPE_PROFILES[key]


# ─── Dashboard ────────────────────────────────────────────────────────────────

def _run_daily_alert_jobs_if_due():
    """
    Safety-net auto runner:
    executes reminder/overdue jobs once per day on first web request.
    Keep cron/task scheduler as primary mechanism.
    """
    today_key = timezone.now().date().isoformat()
    cache_key = f'alerts_daily_jobs_done_{today_key}'
    if cache.get(cache_key):
        return
    try:
        dispatch_hearing_reminders()
        dispatch_overdue_notifications()
    except Exception:
        return
    cache.set(cache_key, True, timeout=26 * 60 * 60)


def _parse_email_lines(raw_text):
    emails = []
    for piece in re.split(r'[\n,;]+', raw_text or ''):
        e = piece.strip().lower()
        if not e:
            continue
        if re.match(r'^[\w\.\+\-]+@[\w\-]+\.[\w\.]{2,}$', e):
            emails.append(e)
    return emails

@login_required
def dashboard(request):
    from .models import GlobalAlertRecipient

    _run_daily_alert_jobs_if_due()
    today         = timezone.now().date()
    next_month    = today + timezone.timedelta(days=30)   # Change 5: 30 days
    cases_qs      = _base_queryset(request.user)

    total_cases    = cases_qs.count()
    inprogress_cases = cases_qs.filter(status='in_progress').count()   # Change 2
    disposed_cases = cases_qs.filter(status='disposed').count()
    on_hold_cases  = cases_qs.filter(status='on_hold').count()

    # Change 5: upcoming hearings for 1 month, not limited to active only
    upcoming_hearings = cases_qs.filter(
        next_hearing_date__gte=today,
        next_hearing_date__lte=next_month,
    ).exclude(status='disposed').order_by('next_hearing_date')

    cases_by_court = (cases_qs.values('court_name')
                      .annotate(count=Count('id')).order_by('-count')[:8])
    cases_by_type  = (cases_qs.values('case_type')
                      .annotate(count=Count('id')).order_by('-count'))

    # Show only newly added cases for today.
    recent_cases = (
        cases_qs.filter(created_at__date=today)
        .select_related('assigned_to')
        .order_by('-created_at')[:10]
    )

    return render(request, 'dashboard/dashboard.html', {
        'total_cases':       total_cases,
        'pending_cases':     inprogress_cases,   # keep template var name for chart
        'inprogress_cases':  inprogress_cases,
        'disposed_cases':    disposed_cases,
        'on_hold_cases':     on_hold_cases,
        'upcoming_hearings': upcoming_hearings,
        'cases_by_court':    list(cases_by_court),
        'cases_by_type':     list(cases_by_type),
        'recent_cases':      recent_cases,
        'new_cases_count':   recent_cases.count(),
        'global_recipients': GlobalAlertRecipient.objects.filter(is_active=True),
        'today':             today,
    })


@login_required
@require_POST
def run_alerts_now(request):
    if request.user.role not in ('admin', 'lawyer') and not request.user.is_superuser:
        messages.error(request, 'Only admin/lawyer can run alert jobs manually.')
        return redirect('dashboard')

    try:
        sent = dispatch_hearing_reminders()
        dispatch_overdue_notifications()
    except Exception as e:
        print("Alert error:", e)
        sent = 0

    messages.success(
        request,
        f'Alert job completed. Hearing emails sent: {sent}.'
    )

    return redirect('dashboard')


@login_required
@require_POST
def manage_global_recipients(request):
    from .models import GlobalAlertRecipient

    if request.user.role not in ('admin', 'lawyer') and not request.user.is_superuser:
        messages.error(request, 'Only admin/lawyer can manage global alert emails.')
        return redirect('dashboard')

    action = request.POST.get('action')
    if action == 'remove':
        rid = request.POST.get('recipient_id')
        GlobalAlertRecipient.objects.filter(pk=rid).delete()
        messages.success(request, 'Global alert recipient removed.')
        return redirect('dashboard')

    raw = request.POST.get('bulk_emails', '')
    emails = _parse_email_lines(raw)
    if not emails:
        messages.error(request, 'Please enter at least one valid email.')
        return redirect('dashboard')

    created = 0
    skipped = 0
    for email in sorted(set(emails)):
        obj, was_created = GlobalAlertRecipient.objects.get_or_create(
            email=email,
            defaults={'added_by': request.user, 'is_active': True},
        )
        if was_created:
            created += 1
            continue
        if not obj.is_active:
            obj.is_active = True
            obj.save(update_fields=['is_active'])
            created += 1
        else:
            skipped += 1

    messages.success(
        request,
        f'Global recipient update complete. Added/reactivated: {created}, already active: {skipped}.',
    )
    return redirect('dashboard')


# ─── Case List ────────────────────────────────────────────────────────────────

@login_required
def case_list(request):
    _run_daily_alert_jobs_if_due()
    filter_form = CaseFilterForm(request.GET)
    cases = _base_queryset(request.user)

    if filter_form.is_valid():
        cases = _apply_filters(cases, filter_form)

    # Change 3: sortable columns
    SORT_MAP = {
        'case_number':       'case_number',
        '-case_number':      '-case_number',
        'title':             'title',
        '-title':            '-title',
        'next_hearing_date': 'next_hearing_date',
        '-next_hearing_date':'-next_hearing_date',
        'filing_date':       'filing_date',
        '-filing_date':      '-filing_date',
        'status':            'status',
        '-status':           '-status',
        'created_at':        'created_at',
        '-created_at':       '-created_at',
    }
    sort = request.GET.get('sort', '-created_at')   # default: latest first
    order_field = SORT_MAP.get(sort, '-created_at')
    cases = cases.select_related('assigned_to').order_by(order_field)
    total_count = cases.count()

    if request.GET.get('export') == 'csv':
        return _export_csv(cases)

    paginator  = Paginator(cases, 15)
    cases_page = paginator.get_page(request.GET.get('page', 1))

    # Build next-sort direction map for toggle links in template
    def next_sort(col):
        if sort == col:
            return f'-{col}'
        return col

    return render(request, 'cases/case_list.html', {
        'cases':           cases_page,
        'filter_form':     filter_form,
        'total_count':     total_count,
        'current_sort':    sort,
        'next_sort':       {
            'case_number':       next_sort('case_number'),
            'title':             next_sort('title'),
            'next_hearing_date': next_sort('next_hearing_date'),
            'filing_date':       next_sort('filing_date'),
            'status':            next_sort('status'),
            'created_at':        next_sort('created_at'),
        },
        'active_statuses': request.GET.getlist('status'),
        'active_projects': request.GET.getlist('project'),
        'active_courts':   request.GET.getlist('court'),
        'active_types':    request.GET.getlist('case_type'),
    })


def _export_csv(queryset):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="cases_export.csv"'
    writer = csv.writer(response)
    writer.writerow([
        'Case Number','Title','Project','Court','Case Type',
        'Petitioner','Respondent','Filing Date',
        'Last Hearing Date','Next Hearing Date',
        'Status','Assigned To','Description','Created At',
    ])
    for c in queryset:
        writer.writerow([
            c.case_number, c.title, c.project or '',
            c.court_name, c.get_case_type_display(),
            c.petitioner, c.respondent,
            c.filing_date.strftime('%d-%m-%Y') if c.filing_date else '',
            c.last_hearing_date.strftime('%d-%m-%Y') if c.last_hearing_date else '',
            c.next_hearing_date.strftime('%d-%m-%Y') if c.next_hearing_date else '',
            c.get_status_display(),
            c.assigned_to.get_full_name() if c.assigned_to else '',
            c.description,
            c.created_at.strftime('%d-%m-%Y %H:%M'),
        ])
    return response


# ─── Case Detail ──────────────────────────────────────────────────────────────

@login_required
def case_detail(request, pk):
    case = get_object_or_404(Case, pk=pk)
    _sync_case_hearing_dates_from_activity(case)
    if request.method == 'POST' and 'add_comment' in request.POST:
        comment_form = CommentForm(request.POST)
        if comment_form.is_valid():
            c = comment_form.save(commit=False)
            c.case = case; c.user = request.user; c.save()
            messages.success(request, 'Comment added.')
            return redirect('case_detail', pk=pk)
    else:
        comment_form = CommentForm()
    from .models import CaseHistory
    case_history = CaseHistory.objects.filter(case=case).select_related('changed_by')[:100]
    court_orders = list(
        case.court_order_links.select_related('created_by').order_by('-order_date', '-pk')[:400]
    )
    updates_qs = (
        case.updates.select_related('created_by')
        .prefetch_related('attachments__document')
        .order_by('-date', '-created_at')
    )
    documents_qs = case.documents.all().order_by('-uploaded_at')
    documents_by_folder = OrderedDict()
    for doc in documents_qs:
        folder_name = (doc.folder or 'Uncategorized').strip() or 'Uncategorized'
        documents_by_folder.setdefault(folder_name, []).append(doc)
    return render(request, 'cases/case_detail.html', {
        'case':               case,
        'case_history':       case_history,
        'court_orders':       court_orders,
        'court_order_count':  case.court_order_links.count(),
        'updates':            updates_qs,
        'documents':          documents_qs,
        'documents_by_folder': documents_by_folder.items(),
        'comments':           case.comments.all().select_related('user'),
        'comment_form':       comment_form,
        'update_form':        CaseUpdateForm(case=case),
        'document_form':      CaseDocumentForm(),
        'deadline_form':      CaseDeadlineForm(),
        'task_form':          CaseTaskForm(),
        'recipient_form':     HearingEmailRecipientForm(),
        'time_form':          TimeEntryForm(),
        'hearing_form':       HearingRecordForm(),
        'court_order_form':   CaseCourtOrderLinkForm(),
    })


# ─── Case Create / Edit ───────────────────────────────────────────────────────

@method_decorator(login_required, name='dispatch')
class CaseCreateView(View):
    template_name = 'cases/case_form.html'

    def get(self, request):
        # Change 4: pre-fill form from paste data in GET param
        initial = {}
        paste = request.GET.get('paste_data')
        if paste:
            try:
                initial = json.loads(paste)
            except Exception:
                pass
        return render(request, self.template_name, {
            'form': CaseForm(initial=initial), 'title': 'Add New Case'
        })

    def post(self, request):
        form = CaseForm(request.POST)
        if form.is_valid():
            case = form.save(commit=False)
            case.created_by = request.user
            case._history_user = request.user
            case.save()
            # Handle calendar email sync
            emails = form.cleaned_data.get('calendar_emails', [])
            if emails and case.next_hearing_date:
                _send_calendar_invites(case, emails, request)
            messages.success(request, f'Case {case.case_number} created successfully.')
            return redirect('case_detail', pk=case.pk)
        return render(request, self.template_name, {'form': form, 'title': 'Add New Case'})


@method_decorator(login_required, name='dispatch')
class CaseEditView(View):
    template_name = 'cases/case_form.html'

    def get(self, request, pk):
        case = get_object_or_404(Case, pk=pk)
        return render(request, self.template_name, {
            'form': CaseForm(instance=case), 'case': case,
            'title': f'Edit Case: {case.case_number}'
        })

    def post(self, request, pk):
        case = get_object_or_404(Case, pk=pk)
        form = CaseForm(request.POST, instance=case)
        if form.is_valid():
            instance = form.save(commit=False)
            instance._history_user = request.user
            instance.save()
            emails = form.cleaned_data.get('calendar_emails', [])
            if emails and case.next_hearing_date:
                _send_calendar_invites(case, emails, request)
            messages.success(request, 'Case updated successfully.')
            return redirect('case_detail', pk=case.pk)
        return render(request, self.template_name, {
            'form': form, 'case': case, 'title': f'Edit Case: {case.case_number}'
        })


@login_required
def case_delete(request, pk):
    if request.user.role not in ('admin',) and not request.user.is_superuser:
        messages.error(request, 'Only admins can delete cases.')
        return redirect('case_detail', pk=pk)
    case = get_object_or_404(Case, pk=pk)
    if request.method == 'POST':
        num = case.case_number; case.delete()
        messages.success(request, f'Case {num} deleted.')
        return redirect('case_list')
    return render(request, 'cases/case_confirm_delete.html', {'case': case})


# ─── Calendar helpers ─────────────────────────────────────────────────────────

def _send_calendar_invites(case, emails, request):
    """
    Logs calendar invite emails. In production, replace with
    Django send_mail + ICS attachment or a calendar API call.
    """
    ics_url = request.build_absolute_uri(f'/cases/{case.pk}/calendar.ics')
    for email in emails:
        # Placeholder — swap for real email sending:
        # send_mail(subject, body, from, [email], attachments=[ics_content])
        messages.info(request, f'📅 Calendar invite queued for {email}')


@login_required
@require_POST
def sync_calendar_emails(request, pk):
    """AJAX endpoint — accepts JSON {emails:[...]} and returns ICS download URL."""
    case = get_object_or_404(Case, pk=pk)
    try:
        data   = json.loads(request.body)
        emails = data.get('emails', [])
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

    if not emails:
        return JsonResponse({'ok': False, 'error': 'No emails provided'}, status=400)

    if not case.next_hearing_date:
        return JsonResponse({'ok': False, 'error': 'No hearing date set for this case'}, status=400)

    import re
    pattern = re.compile(r'^[\w\.\+\-]+@[\w\-]+\.[\w\.]{2,}$')
    bad = [e for e in emails if not pattern.match(e.strip())]
    if bad:
        return JsonResponse({'ok': False, 'error': f'Invalid emails: {", ".join(bad)}'}, status=400)

    ics_url = request.build_absolute_uri(f'/cases/{case.pk}/calendar.ics')
    gcal_url = (
        'https://calendar.google.com/calendar/render?action=TEMPLATE'
        f'&text={case.title}+Hearing'
        f'&dates={case.next_hearing_date.strftime("%Y%m%d")}/{case.next_hearing_date.strftime("%Y%m%d")}'
        f'&details=Case+No:+{case.case_number}'
        f'&location={case.court_name}'
    )
    # In production: iterate emails and send_mail with ICS attachment
    return JsonResponse({
        'ok':       True,
        'ics_url':  ics_url,
        'gcal_url': gcal_url,
        'emails':   emails,
        'message':  f'Calendar invite links generated for {len(emails)} recipient(s).',
    })


# ─── Case Updates ─────────────────────────────────────────────────────────────

@login_required
def add_case_update(request, pk):
    case = get_object_or_404(Case, pk=pk)
    if request.method == 'POST':
        form = CaseUpdateForm(request.POST, case=case)
        if form.is_valid():
            u = form.save(commit=False)
            u.case = case; u.created_by = request.user; u.save()
            _sync_case_hearing_dates_from_activity(case)
            messages.success(request, 'Case update added.')
        else:
            messages.error(request, 'Invalid update data.')
    return redirect('case_detail', pk=pk)


@login_required
@require_POST
def update_case_update_details(request, pk, update_pk):
    case = get_object_or_404(Case, pk=pk)
    update = get_object_or_404(CaseUpdate, pk=update_pk, case=case)

    update.order_details = (request.POST.get('order_details') or '').strip()
    update.filing_response = (request.POST.get('filing_response') or '').strip()
    update.response_received = (request.POST.get('response_received') or '').strip()
    update.internal_notes = (request.POST.get('internal_notes') or '').strip()
    update.save(update_fields=['order_details', 'filing_response', 'response_received', 'internal_notes'])

    selected_ids = request.POST.getlist('timeline_documents')
    valid_docs = case.documents.filter(pk__in=selected_ids)
    CaseUpdateDocumentAttachment.objects.filter(case_update=update).exclude(document__in=valid_docs).delete()
    existing_ids = set(
        CaseUpdateDocumentAttachment.objects.filter(case_update=update).values_list('document_id', flat=True)
    )
    for doc in valid_docs:
        if doc.pk in existing_ids:
            continue
        CaseUpdateDocumentAttachment.objects.create(
            case_update=update, document=doc, added_by=request.user
        )

    messages.success(request, 'Timeline entry updated.')
    return redirect(f"{reverse('case_detail', kwargs={'pk': pk})}#timeline")


@login_required
def delete_case_update(request, pk, update_pk):
    update = get_object_or_404(CaseUpdate, pk=update_pk, case_id=pk)
    if request.method == 'POST':
        update.delete()
        _sync_case_hearing_dates_from_activity(update.case)
        messages.success(request, 'Update deleted.')
    return redirect('case_detail', pk=pk)


# ─── Documents ────────────────────────────────────────────────────────────────

@login_required
def upload_document(request, pk):
    case = get_object_or_404(Case, pk=pk)
    if request.method == 'POST':
        form = CaseDocumentForm(request.POST, request.FILES)
        if form.is_valid():
            doc = form.save(commit=False)
            doc.case = case; doc.uploaded_by = request.user; doc.save()
            log_document_event(case, f'Uploaded: {doc.title or doc.filename} ({doc.get_document_type_display()})', user=request.user)
            messages.success(request, 'Document uploaded successfully.')
        else:
            for errs in form.errors.values():
                for e in errs: messages.error(request, e)
    return redirect('case_detail', pk=pk)


@login_required
def download_document(request, doc_pk):
    doc = get_object_or_404(CaseDocument, pk=doc_pk)
    try:
        return FileResponse(open(doc.file.path,'rb'), as_attachment=True, filename=doc.filename)
    except FileNotFoundError:
        raise Http404("Document file not found.")


@login_required
def delete_document(request, pk, doc_pk):
    doc = get_object_or_404(CaseDocument, pk=doc_pk, case_id=pk)
    if request.method == 'POST':
        if doc.file and os.path.exists(doc.file.path):
            os.remove(doc.file.path)
        log_document_event(doc.case, f'Deleted: {doc.filename}', user=request.user)
        doc.delete()
        messages.success(request, 'Document deleted.')
    return redirect('case_detail', pk=pk)


@login_required
def manage_documents(request):
    """
    Centralized document page for:
    - case-linked uploads (same records as case detail)
    - bulk uploads
    - offline document registry
    """
    visible_cases = _base_queryset(request.user).order_by('-created_at')
    visible_case_ids = visible_cases.values_list('id', flat=True)
    uploaded_case_ids = (
        CaseDocument.objects.filter(case_id__in=visible_case_ids)
        .values_list('case_id', flat=True)
        .distinct()
    )
    uploaded_cases = visible_cases.filter(id__in=uploaded_case_ids)

    q = (request.GET.get('q') or '').strip()
    case_filter = request.GET.get('case')
    doc_type = request.GET.get('document_type')
    nav_case = (request.GET.get('nav_case') or '').strip()
    nav_folder = (request.GET.get('nav_folder') or '').strip()

    docs_qs = CaseDocument.objects.select_related('case', 'uploaded_by').filter(case_id__in=visible_case_ids)
    if q:
        docs_qs = docs_qs.filter(
            Q(title__icontains=q) | Q(notes__icontains=q) |
            Q(folder__icontains=q) | Q(tags__icontains=q) |
            Q(case__title__icontains=q) | Q(case__case_number__icontains=q) |
            Q(file__icontains=q)
        )
    # Case navigation for the Case → Folder → Files UI
    if not nav_case and case_filter:
        nav_case = str(case_filter)
    if nav_case:
        docs_qs = docs_qs.filter(case_id=nav_case)
    if doc_type:
        docs_qs = docs_qs.filter(document_type=doc_type)

    offline_qs = OfflineDocument.objects.select_related('case', 'uploaded_by')
    if request.user.role == 'lawyer':
        offline_qs = offline_qs.filter(Q(case_id__in=visible_case_ids) | Q(uploaded_by=request.user))
    if q:
        offline_qs = offline_qs.filter(
            Q(title__icontains=q) | Q(notes__icontains=q) |
            Q(folder__icontains=q) | Q(tags__icontains=q) |
            Q(storage_location__icontains=q) |
            Q(case__title__icontains=q) | Q(case__case_number__icontains=q)
        )
    if case_filter:
        offline_qs = offline_qs.filter(case_id=case_filter)

    documents = list(docs_qs.order_by('folder', '-uploaded_at')[:500])
    docs_by_folder = OrderedDict()
    for d in documents:
        folder_name = (d.folder or 'Uncategorized').strip() or 'Uncategorized'
        docs_by_folder.setdefault(folder_name, []).append(d)
    case_folders_map = {}
    for c in uploaded_cases:
        folders = (
            CaseDocument.objects.filter(case=c)
            .exclude(folder__isnull=True)
            .exclude(folder__exact='')
            .values_list('folder', flat=True)
            .distinct()
            .order_by('folder')
        )
        case_folders_map[str(c.pk)] = list(folders)

    return render(request, 'cases/manage_documents.html', {
        'documents': documents,
        'documents_by_folder': docs_by_folder.items(),
        'offline_documents': offline_qs.order_by('-created_at')[:500],
        'cases_for_select': uploaded_cases[:500],
        'bulk_upload_form': BulkCaseDocumentUploadForm(case_queryset=visible_cases),
        'offline_form': OfflineDocumentForm(case_queryset=visible_cases),
        'q': q,
        'selected_case': case_filter or '',
        'selected_document_type': doc_type or '',
        'document_type_choices': CaseDocument.DOCUMENT_TYPE_CHOICES,
        'case_folders_map': case_folders_map,
        'nav_case': nav_case,
    })


@login_required
@require_POST
def bulk_upload_case_documents(request):
    visible_cases = _base_queryset(request.user)
    form = BulkCaseDocumentUploadForm(request.POST, request.FILES, case_queryset=visible_cases)
    files = request.FILES.getlist('files')
    if not form.is_valid() or not files:
        messages.error(request, 'Please choose a valid case and at least one file.')
        return redirect('manage_documents')

    case = form.cleaned_data['case']
    document_type = form.cleaned_data['document_type']
    title_prefix = (form.cleaned_data.get('title_prefix') or '').strip()
    notes = form.cleaned_data.get('notes') or ''
    folder = (form.cleaned_data.get('resolved_folder') or '').strip()
    tags = (form.cleaned_data.get('tags') or '').strip()

    created = 0
    for f in files:
        title = f'{title_prefix} {f.name}'.strip() if title_prefix else f.name
        doc = CaseDocument.objects.create(
            case=case,
            title=title[:200],
            document_type=document_type,
            folder=folder,
            tags=tags,
            file=f,
            notes=notes,
            uploaded_by=request.user,
        )
        log_document_event(case, f'Uploaded: {doc.title or doc.filename} ({doc.get_document_type_display()})', user=request.user)
        created += 1

    messages.success(request, f'{created} document(s) uploaded to {case.case_number}.')
    return redirect('manage_documents')


@login_required
@require_POST
def delete_case_document_from_manager(request, doc_pk):
    doc = get_object_or_404(CaseDocument, pk=doc_pk)
    if request.user.role == 'lawyer' and doc.case.assigned_to_id != request.user.id:
        messages.error(request, 'You do not have permission to delete this document.')
        return redirect('manage_documents')

    case = doc.case
    filename = doc.filename
    if doc.file and os.path.exists(doc.file.path):
        os.remove(doc.file.path)
    doc.delete()
    log_document_event(case, f'Deleted: {filename}', user=request.user)
    messages.success(request, 'Document deleted.')
    return redirect('manage_documents')


def _manager_accessible_documents(user):
    visible_cases = _base_queryset(user).values_list('id', flat=True)
    return CaseDocument.objects.filter(case_id__in=visible_cases).select_related('case')


@login_required
@require_POST
def bulk_case_document_actions(request):
    action = (request.POST.get('action') or '').strip()
    selected = request.POST.getlist('selected_docs')
    if not selected:
        messages.warning(request, 'Please select at least one document.')
        return redirect('manage_documents')

    docs = _manager_accessible_documents(request.user).filter(pk__in=selected)
    if not docs.exists():
        messages.error(request, 'No valid documents found for selected rows.')
        return redirect('manage_documents')

    if action == 'delete':
        deleted = 0
        for doc in docs:
            case = doc.case
            filename = doc.filename
            if doc.file and os.path.exists(doc.file.path):
                os.remove(doc.file.path)
            doc.delete()
            log_document_event(case, f'Deleted: {filename}', user=request.user)
            deleted += 1
        messages.success(request, f'{deleted} document(s) deleted.')
        return redirect('manage_documents')

    if action == 'download':
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for doc in docs.order_by('case__case_number', 'id'):
                if not doc.file:
                    continue
                try:
                    with open(doc.file.path, 'rb') as fh:
                        file_bytes = fh.read()
                except FileNotFoundError:
                    continue
                safe_name = doc.filename or f'document_{doc.pk}'
                zf.writestr(f'{doc.case.case_number}/{safe_name}', file_bytes)
        buffer.seek(0)
        resp = HttpResponse(buffer.getvalue(), content_type='application/zip')
        resp['Content-Disposition'] = 'attachment; filename="selected_documents.zip"'
        return resp

    messages.error(request, 'Unsupported bulk action.')
    return redirect('manage_documents')


@login_required
@require_POST
def add_offline_document(request):
    visible_cases = _base_queryset(request.user)
    form = OfflineDocumentForm(request.POST, request.FILES, case_queryset=visible_cases)
    if not form.is_valid():
        messages.error(request, 'Please fix offline document form errors.')
        return redirect('manage_documents')

    doc = form.save(commit=False)
    doc.uploaded_by = request.user
    doc.save()
    messages.success(request, 'Offline document entry saved.')
    return redirect('manage_documents')


@login_required
@require_POST
def delete_offline_document(request, offline_pk):
    doc = get_object_or_404(OfflineDocument, pk=offline_pk)
    if request.user.role == 'lawyer' and doc.uploaded_by_id != request.user.id:
        messages.error(request, 'You do not have permission to delete this offline document.')
        return redirect('manage_documents')
    if doc.file and os.path.exists(doc.file.path):
        os.remove(doc.file.path)
    doc.delete()
    messages.success(request, 'Offline document deleted.')
    return redirect('manage_documents')


@login_required
def download_offline_document(request, offline_pk):
    doc = get_object_or_404(OfflineDocument, pk=offline_pk)
    if not doc.file:
        raise Http404('No digital file attached for this offline document.')
    try:
        return FileResponse(open(doc.file.path, 'rb'), as_attachment=True, filename=doc.filename)
    except FileNotFoundError:
        raise Http404('Offline document file not found.')


@login_required
def download_case_documents_zip(request, case_pk):
    case = get_object_or_404(Case, pk=case_pk)
    if request.user.role == 'lawyer' and case.assigned_to_id != request.user.id:
        messages.error(request, 'You do not have permission for this case.')
        return redirect('manage_documents')

    docs = case.documents.all().order_by('id')
    if not docs.exists():
        messages.warning(request, 'No documents available for this case.')
        return redirect('manage_documents')

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            if not doc.file:
                continue
            try:
                with open(doc.file.path, 'rb') as fh:
                    file_bytes = fh.read()
            except FileNotFoundError:
                continue
            safe_name = doc.filename or f'document_{doc.pk}'
            zf.writestr(f'{case.case_number}/{safe_name}', file_bytes)

    buffer.seek(0)
    resp = HttpResponse(buffer.getvalue(), content_type='application/zip')
    resp['Content-Disposition'] = f'attachment; filename="{case.case_number}_documents.zip"'
    return resp


@login_required
@require_POST
def import_offline_documents_csv(request):
    """
    CSV import for offline document register (metadata-focused).
    Required headers: title
    Optional headers: case_number, folder, tags, storage_location, notes
    """
    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        messages.error(request, 'Please upload a CSV file.')
        return redirect('manage_documents')

    try:
        decoded = csv_file.read().decode('utf-8-sig')
    except Exception:
        messages.error(request, 'Invalid CSV encoding. Use UTF-8.')
        return redirect('manage_documents')

    reader = csv.DictReader(io.StringIO(decoded))
    if not reader.fieldnames or 'title' not in [h.strip() for h in reader.fieldnames]:
        messages.error(request, 'CSV must include at least: title')
        return redirect('manage_documents')

    visible_cases = _base_queryset(request.user)
    case_map = {c.case_number.strip().lower(): c for c in visible_cases}

    created = 0
    skipped = 0
    for row in reader:
        title = (row.get('title') or '').strip()
        if not title:
            skipped += 1
            continue
        case_number = (row.get('case_number') or '').strip().lower()
        case = case_map.get(case_number) if case_number else None
        OfflineDocument.objects.create(
            case=case,
            title=title[:220],
            folder=(row.get('folder') or '').strip()[:100],
            tags=(row.get('tags') or '').strip()[:300],
            storage_location=(row.get('storage_location') or '').strip()[:300],
            notes=(row.get('notes') or '').strip(),
            uploaded_by=request.user,
        )
        created += 1

    messages.success(request, f'CSV import done. Created: {created}, skipped: {skipped}.')
    return redirect('manage_documents')


# ─── Comments ─────────────────────────────────────────────────────────────────

@login_required
def delete_comment(request, pk, comment_pk):
    comment = get_object_or_404(Comment, pk=comment_pk, case_id=pk)
    if request.user == comment.user or request.user.role == 'admin':
        if request.method == 'POST':
            comment.delete()
            messages.success(request, 'Comment deleted.')
    else:
        messages.error(request, 'You cannot delete this comment.')
    return redirect('case_detail', pk=pk)


# ─── ICS Calendar Export ──────────────────────────────────────────────────────

@login_required
def export_hearing_ics(request, pk):
    case = get_object_or_404(Case, pk=pk)
    if not case.next_hearing_date:
        messages.error(request, 'This case has no upcoming hearing date.')
        return redirect('case_detail', pk=pk)
    uid       = str(uuid.uuid4())
    now_stamp = timezone.now().strftime('%Y%m%dT%H%M%SZ')
    date_str  = case.next_hearing_date.strftime('%Y%m%d')
    desc      = (f"Case No: {case.case_number}\\nCourt: {case.court_name}\\n"
                 f"Type: {case.get_case_type_display()}\\nStatus: {case.get_status_display()}\\n"
                 f"Parties: {case.petitioner} vs {case.respondent}")
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//LegalCRM//EN\r\n"
        "CALSCALE:GREGORIAN\r\nMETHOD:PUBLISH\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:{now_stamp}\r\n"
        f"DTSTART;VALUE=DATE:{date_str}\r\nDTEND;VALUE=DATE:{date_str}\r\n"
        f"SUMMARY:Hearing: {case.title}\r\nDESCRIPTION:{desc}\r\n"
        f"LOCATION:{case.court_name}\r\nSTATUS:CONFIRMED\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    safe = case.case_number.replace('/','_')
    resp = HttpResponse(ics, content_type='text/calendar; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="hearing_{safe}.ics"'
    return resp


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 1 — NEW VIEWS
# ═══════════════════════════════════════════════════════════════════════════

from .models import (CaseDeadline, CaseTask, HearingEmailRecipient, Notification)
from .forms  import (CaseDeadlineForm, CaseTaskForm, HearingEmailRecipientForm)
from .services import (notify_user, notify_team_about_task, notify_deadline_created,
                       send_hearing_alert_to_recipients, send_hearing_alert_to_emails,
                       on_hearing_date_changed)
from .services import dispatch_hearing_reminders, dispatch_overdue_notifications


# ── Workload Dashboard ────────────────────────────────────────────────────

@login_required
def workload_dashboard(request):
    """Director-level view: all lawyers, their case loads, tasks, deadlines."""
    from accounts.models import User
    today = timezone.now().date()
    next_week = today + timezone.timedelta(days=7)

    lawyers = User.objects.filter(role__in=['lawyer','admin']).order_by('first_name')

    workload = []
    for lawyer in lawyers:
        cases = Case.objects.filter(assigned_to=lawyer)
        open_cases      = cases.exclude(status='disposed').count()
        inprogress      = cases.filter(status='in_progress').count()
        on_hold         = cases.filter(status='on_hold').count()
        hearings_week   = cases.filter(
            next_hearing_date__gte=today,
            next_hearing_date__lte=next_week
        ).count()
        overdue_dl      = CaseDeadline.objects.filter(
            case__assigned_to=lawyer, due_date__lt=today, is_completed=False
        ).count()
        pending_tasks   = CaseTask.objects.filter(
            assigned_to=lawyer, is_completed=False
        ).count()
        workload.append({
            'lawyer':         lawyer,
            'open_cases':     open_cases,
            'inprogress':     inprogress,
            'on_hold':        on_hold,
            'hearings_week':  hearings_week,
            'overdue_dl':     overdue_dl,
            'pending_tasks':  pending_tasks,
        })

    # Upcoming hearings across all cases (next 30 days)
    all_hearings = Case.objects.filter(
        next_hearing_date__gte=today,
        next_hearing_date__lte=today + timezone.timedelta(days=30)
    ).exclude(status='disposed').order_by('next_hearing_date').select_related('assigned_to')

    # All overdue deadlines
    overdue_deadlines = CaseDeadline.objects.filter(
        due_date__lt=today, is_completed=False
    ).select_related('case', 'case__assigned_to').order_by('due_date')[:20]

    # Stale cases — no update in 45 days
    stale_threshold = today - timezone.timedelta(days=45)
    stale_cases = Case.objects.filter(
        updated_at__date__lte=stale_threshold
    ).exclude(status='disposed').select_related('assigned_to').order_by('updated_at')[:15]

    return render(request, 'cases/workload_dashboard.html', {
        'workload':          workload,
        'all_hearings':      all_hearings,
        'overdue_deadlines': overdue_deadlines,
        'stale_cases':       stale_cases,
        'today':             today,
        'next_week':         next_week,
    })


# ── Notifications ─────────────────────────────────────────────────────────

@login_required
def notification_list(request):
    """Full notification centre page."""
    notifs = Notification.objects.filter(user=request.user)
    # Mark all as read when page is opened
    notifs.filter(is_read=False).update(is_read=True)
    return render(request, 'cases/notifications.html', {'notifications': notifs})


@login_required
@require_POST
def mark_notification_read(request, notif_pk):
    n = get_object_or_404(Notification, pk=notif_pk, user=request.user)
    n.is_read = True; n.save()
    return JsonResponse({'ok': True})


@login_required
@require_POST
def mark_all_notifications_read(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return JsonResponse({'ok': True})


@login_required
def notification_count_api(request):
    """AJAX endpoint returning unread count for topbar badge."""
    count = Notification.objects.filter(user=request.user, is_read=False).count()
    recent = list(Notification.objects.filter(user=request.user).values(
        'id','notif_type','title','message','link','is_read','created_at'
    )[:8])
    for n in recent:
        n['created_at'] = n['created_at'].strftime('%d %b, %H:%M')
    return JsonResponse({'count': count, 'notifications': recent})


# ── Deadlines ─────────────────────────────────────────────────────────────

@login_required
def add_deadline(request, pk):
    case = get_object_or_404(Case, pk=pk)
    if request.method == 'POST':
        form = CaseDeadlineForm(request.POST)
        if form.is_valid():
            dl = form.save(commit=False)
            dl.case = case; dl.created_by = request.user
            dl.save()
            log_deadline_event(case, dl.title, 'added', user=request.user)
            notify_deadline_created(dl)
            messages.success(request, f'Deadline "{dl.title}" added.')
        else:
            messages.error(request, 'Please fix the deadline form errors.')
    return redirect(f'/cases/{pk}/#deadlines')


@login_required
@require_POST
def complete_deadline(request, pk, dl_pk):
    dl = get_object_or_404(CaseDeadline, pk=dl_pk, case_id=pk)
    dl.is_completed = True
    dl.completed_at = timezone.now()
    dl.completed_by = request.user
    dl.save()
    log_deadline_event(dl.case, dl.title, 'completed', user=request.user)
    messages.success(request, f'Deadline "{dl.title}" marked complete.')
    return redirect(f'/cases/{pk}/#deadlines')


@login_required
@require_POST
def delete_deadline(request, pk, dl_pk):
    dl = get_object_or_404(CaseDeadline, pk=dl_pk, case_id=pk)
    dl.delete()
    messages.success(request, 'Deadline deleted.')
    return redirect(f'/cases/{pk}/#deadlines')


# ── Tasks ─────────────────────────────────────────────────────────────────

@login_required
def add_task(request, pk):
    case = get_object_or_404(Case, pk=pk)
    if request.method == 'POST':
        form = CaseTaskForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            task.case = case; task.created_by = request.user
            task.save()
            log_task_event(case, task.title, 'added', user=request.user)
            notify_team_about_task(task)
            messages.success(request, f'Task "{task.title}" added.')
        else:
            messages.error(request, 'Please fix the task form errors.')
    return redirect(f'/cases/{pk}/#tasks')


@login_required
@require_POST
def complete_task(request, pk, task_pk):
    task = get_object_or_404(CaseTask, pk=task_pk, case_id=pk)
    task.is_completed = True
    task.completed_at = timezone.now()
    task.save()
    log_task_event(task.case, task.title, 'completed', user=request.user)
    messages.success(request, f'Task "{task.title}" completed.')
    return redirect(f'/cases/{pk}/#tasks')


@login_required
@require_POST
def delete_task(request, pk, task_pk):
    task = get_object_or_404(CaseTask, pk=task_pk, case_id=pk)
    task.delete()
    messages.success(request, 'Task deleted.')
    return redirect(f'/cases/{pk}/#tasks')


# ── Hearing Email Recipients ──────────────────────────────────────────────

@login_required
def manage_recipients(request, pk):
    """Add/remove email recipients, and manually trigger send."""
    case = get_object_or_404(Case, pk=pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            form = HearingEmailRecipientForm(request.POST)
            if form.is_valid():
                try:
                    r = form.save(commit=False)
                    r.case = case; r.added_by = request.user
                    r.save()
                    messages.success(request, f'Added {r.email} to hearing alerts.')
                except Exception:
                    messages.error(request, 'That email is already added to this case.')
            else:
                messages.error(request, 'Invalid email address.')

        elif action == 'remove':
            rid = request.POST.get('recipient_id')
            HearingEmailRecipient.objects.filter(pk=rid, case=case).delete()
            messages.success(request, 'Recipient removed.')

        elif action == 'send_now':
            sent, failed = send_hearing_alert_to_recipients(case, trigger='manual')
            if sent:
                messages.success(request, f'✓ Hearing alert sent to {sent} recipient(s).')
            if failed:
                messages.error(request, f'Failed for: {", ".join(failed)}')
            if not sent and not failed:
                messages.warning(request, 'No active recipients. Add email addresses first.')

    return redirect(f'/cases/{pk}/#recipients')


# ── Override sync_calendar_emails to also send real emails ───────────────

@login_required
@require_POST
def sync_calendar_emails(request, pk):
    """Send hearing alert emails + return ICS/GCal links."""
    case = get_object_or_404(Case, pk=pk)
    try:
        data   = json.loads(request.body)
        emails = [e.strip() for e in data.get('emails', []) if e.strip()]
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

    if not emails:
        return JsonResponse({'ok': False, 'error': 'No emails provided'}, status=400)
    if not case.next_hearing_date:
        return JsonResponse({'ok': False, 'error': 'No hearing date set'}, status=400)

    import re
    bad = [e for e in emails if not re.match(r'^[\w\.\+\-]+@[\w\-]+\.[\w\.]{2,}$', e)]
    if bad:
        return JsonResponse({'ok': False, 'error': f'Invalid: {", ".join(bad)}'}, status=400)

    sent, failed = send_hearing_alert_to_emails(case, emails)

    ics_url  = request.build_absolute_uri(f'/cases/{case.pk}/calendar.ics')
    gcal_url = (
        'https://calendar.google.com/calendar/render?action=TEMPLATE'
        f'&text={case.title}+Hearing'
        f'&dates={case.next_hearing_date.strftime("%Y%m%d")}/{case.next_hearing_date.strftime("%Y%m%d")}'
        f'&details=Case+No:+{case.case_number}&location={case.court_name}'
    )
    msg = f'Emails sent to {sent} recipient(s).'
    if failed:
        msg += f' Failed: {", ".join(failed)}'

    return JsonResponse({'ok': True, 'ics_url': ics_url, 'gcal_url': gcal_url,
                         'emails': emails, 'message': msg})


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 — Director Analytics
# ═══════════════════════════════════════════════════════════════════════════

from .models  import CaseOutcome, TimeEntry
from .forms   import CaseOutcomeForm, TimeEntryForm, AnalyticsFilterForm


# ── Analytics Dashboard ───────────────────────────────────────────────────

@login_required
def analytics_dashboard(request):
    """
    Director-level analytics report.
    Win/loss rates, case velocity, time logged, court performance.
    """
    from django.db.models import Sum, Avg, F, ExpressionWrapper, DurationField
    from accounts.models import User

    filter_form = AnalyticsFilterForm(request.GET)
    today = timezone.now().date()

    # Date range — default: last 12 months
    from_date = today.replace(month=1, day=1)   # Jan 1 this year
    to_date   = today
    lawyer_id = None
    case_type_filter = None

    if filter_form.is_valid():
        from_date = filter_form.cleaned_data.get('from_date') or from_date
        to_date   = filter_form.cleaned_data.get('to_date')   or to_date
        lawyer_id = filter_form.cleaned_data.get('lawyer') or None
        case_type_filter = filter_form.cleaned_data.get('case_type') or None

    # Base querysets filtered by date and optionally lawyer/type
    outcomes_qs = CaseOutcome.objects.filter(
        disposed_date__gte=from_date,
        disposed_date__lte=to_date,
    )
    cases_qs = Case.objects.filter(
        filing_date__gte=from_date,
        filing_date__lte=to_date,
    )
    if lawyer_id:
        outcomes_qs = outcomes_qs.filter(case__assigned_to_id=lawyer_id)
        cases_qs    = cases_qs.filter(assigned_to_id=lawyer_id)
    if case_type_filter:
        outcomes_qs = outcomes_qs.filter(case__case_type=case_type_filter)
        cases_qs    = cases_qs.filter(case_type=case_type_filter)

    # ── Outcome stats ───────────────────────────────────────────────────
    total_outcomes = outcomes_qs.count()
    outcomes_by_result = list(
        outcomes_qs.values('result')
                   .annotate(count=Count('id'))
                   .order_by('-count')
    )
    won_count       = outcomes_qs.filter(result='won').count()
    lost_count      = outcomes_qs.filter(result='lost').count()
    settled_count   = outcomes_qs.filter(result='settled').count()
    win_rate        = round((won_count / total_outcomes * 100), 1) if total_outcomes else 0
    settlement_rate = round((settled_count / total_outcomes * 100), 1) if total_outcomes else 0

    # ── Win rate by lawyer ──────────────────────────────────────────────
    lawyers = User.objects.filter(role__in=['lawyer','admin'])
    lawyer_stats = []
    for lawyer in lawyers:
        lo = outcomes_qs.filter(case__assigned_to=lawyer)
        total = lo.count()
        if total == 0:
            continue
        won = lo.filter(result='won').count()
        lawyer_stats.append({
            'lawyer':    lawyer,
            'total':     total,
            'won':       won,
            'lost':      lo.filter(result='lost').count(),
            'settled':   lo.filter(result='settled').count(),
            'win_rate':  round(won / total * 100, 1),
        })
    lawyer_stats.sort(key=lambda x: x['win_rate'], reverse=True)

    # ── Win rate by court ───────────────────────────────────────────────
    court_stats = []
    courts = outcomes_qs.values_list('case__court_name', flat=True).distinct()
    for court in courts:
        co = outcomes_qs.filter(case__court_name=court)
        total = co.count()
        won   = co.filter(result='won').count()
        court_stats.append({
            'court':    court,
            'total':    total,
            'won':      won,
            'win_rate': round(won / total * 100, 1) if total else 0,
        })
    court_stats.sort(key=lambda x: x['win_rate'], reverse=True)

    # ── Win rate by case type ───────────────────────────────────────────
    type_stats = []
    for ct_val, ct_label in Case.CASE_TYPE_CHOICES:
        co = outcomes_qs.filter(case__case_type=ct_val)
        total = co.count()
        if total == 0:
            continue
        won = co.filter(result='won').count()
        type_stats.append({
            'type_val':  ct_val,
            'type_label':ct_label,
            'total':     total,
            'won':       won,
            'win_rate':  round(won / total * 100, 1),
        })
    type_stats.sort(key=lambda x: x['win_rate'], reverse=True)

    # ── Case filing trend (monthly) ─────────────────────────────────────
    from django.db.models.functions import TruncMonth
    monthly_filings = list(
        cases_qs.annotate(month=TruncMonth('filing_date'))
                .values('month')
                .annotate(count=Count('id'))
                .order_by('month')
    )
    for m in monthly_filings:
        m['month_label'] = m['month'].strftime('%b %Y') if m['month'] else ''

    # ── Time entries ────────────────────────────────────────────────────
    time_qs = TimeEntry.objects.filter(
        date__gte=from_date, date__lte=to_date
    )
    if lawyer_id:
        time_qs = time_qs.filter(user_id=lawyer_id)
    total_hours    = time_qs.aggregate(t=Sum('hours'))['t'] or 0
    billable_hours = time_qs.filter(is_billable=True).aggregate(t=Sum('hours'))['t'] or 0

    time_by_lawyer = list(
        time_qs.values('user__first_name','user__last_name','user__username')
               .annotate(
    total_hours=Sum('hours'),
    billable_hours=Sum('hours', filter=Q(is_billable=True))
)
               .order_by('-total_hours')
    )

    # ── Recent outcomes ─────────────────────────────────────────────────
    recent_outcomes = outcomes_qs.select_related('case','case__assigned_to').order_by('-disposed_date')[:10]

    # ── Case age distribution ───────────────────────────────────────────
    open_cases = Case.objects.exclude(status='disposed')
    age_buckets = {
        'under_30':  open_cases.filter(filing_date__gte=today - timezone.timedelta(days=30)).count(),
        '30_to_90':  open_cases.filter(filing_date__lt=today - timezone.timedelta(days=30),
                                       filing_date__gte=today - timezone.timedelta(days=90)).count(),
        '90_to_180': open_cases.filter(filing_date__lt=today - timezone.timedelta(days=90),
                                       filing_date__gte=today - timezone.timedelta(days=180)).count(),
        'over_180':  open_cases.filter(filing_date__lt=today - timezone.timedelta(days=180)).count(),
    }

    return render(request, 'cases/analytics.html', {
        'filter_form':      filter_form,
        'from_date':        from_date,
        'to_date':          to_date,
        'total_outcomes':   total_outcomes,
        'outcomes_by_result': outcomes_by_result,
        'won_count':        won_count,
        'lost_count':       lost_count,
        'settled_count':    settled_count,
        'win_rate':         win_rate,
        'settlement_rate':  settlement_rate,
        'lawyer_stats':     lawyer_stats,
        'court_stats':      court_stats[:8],
        'type_stats':       type_stats,
        'monthly_filings':  monthly_filings,
        'total_hours':      float(total_hours),
        'billable_hours':   float(billable_hours),
        'time_by_lawyer':   time_by_lawyer,
        'recent_outcomes':  recent_outcomes,
        'age_buckets':      age_buckets,
        'today':            today,
    })


# ── Record Case Outcome ───────────────────────────────────────────────────

@login_required
def record_outcome(request, pk):
    """Record win/loss/settlement when a case is disposed."""
    case = get_object_or_404(Case, pk=pk)

    # Check if outcome already recorded
    existing = getattr(case, 'outcome', None)

    if request.method == 'POST':
        form = CaseOutcomeForm(request.POST, instance=existing)
        if form.is_valid():
            outcome = form.save(commit=False)
            outcome.case        = case
            outcome.recorded_by = request.user
            outcome.save()
            # Auto-set case status to disposed
            if case.status != 'disposed':
                case.status = 'disposed'
                case.save(update_fields=['status'])
            log_outcome_event(
                case,
                outcome.get_result_display(),
                user=request.user,
                judge_name=outcome.judge_name or '',
                opposing_counsel=outcome.opposing_counsel or '',
            )
            messages.success(request, f'Outcome recorded: {outcome.get_result_display()}')

            # Notify all lawyers + director
            from .services import notify_user
            from accounts.models import User
            for admin_user in User.objects.filter(role__in=['admin']):
                notify_user(
                    user=admin_user,
                    notif_type='update',
                    title=f'Case closed: {outcome.get_result_display()} — {case.case_number}',
                    message=f'{case.title} · {case.court_name}',
                    case=case,
                )
            return redirect('case_detail', pk=pk)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = CaseOutcomeForm(instance=existing)

    return render(request, 'cases/record_outcome.html', {
        'form': form, 'case': case, 'existing': existing,
    })


# ── Time Entries ──────────────────────────────────────────────────────────

@login_required
def add_time_entry(request, pk):
    """Log time spent on a case."""
    case = get_object_or_404(Case, pk=pk)
    if request.method == 'POST':
        form = TimeEntryForm(request.POST)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.case = case
            entry.user = request.user
            entry.save()
            messages.success(request, f'{entry.hours}h logged.')
        else:
            messages.error(request, 'Invalid time entry.')
    return redirect(f'/cases/{pk}/#timelog')


@login_required
@require_POST
def delete_time_entry(request, pk, entry_pk):
    entry = get_object_or_404(TimeEntry, pk=entry_pk, case_id=pk)
    if request.user == entry.user or request.user.role == 'admin':
        entry.delete()
        messages.success(request, 'Time entry deleted.')
    return redirect(f'/cases/{pk}/#timelog')


# ── Billing summary export ────────────────────────────────────────────────

@login_required
def export_billing_csv(request):
    """Export all time entries as a CSV billing report."""
    today = timezone.now().date()
    from_date = request.GET.get('from', str(today.replace(day=1)))
    to_date   = request.GET.get('to',   str(today))

    entries = TimeEntry.objects.filter(
        date__gte=from_date, date__lte=to_date
    ).select_related('case', 'user').order_by('case__case_number', 'date')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="billing_{from_date}_to_{to_date}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Case No.','Case Title','Lawyer','Date','Hours','Billable','Description'])
    for e in entries:
        writer.writerow([
            e.case.case_number, e.case.title,
            e.user.get_full_name() if e.user else '',
            e.date.strftime('%d-%m-%Y'),
            float(e.hours), 'Yes' if e.is_billable else 'No',
            e.description,
        ])
    return response


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 3 — Client Portal, AI Summary, Document Generator
# ═══════════════════════════════════════════════════════════════════════════

from .models import ClientCase
from .forms  import ClientGrantForm, DocumentTemplateForm


# ── Client Portal ─────────────────────────────────────────────────────────

@login_required
def client_portal(request):
    """
    Read-only portal for clients.
    Shows only their linked cases — no internal notes, no comments.
    """
    if request.user.role != 'client':
        return redirect('dashboard')

    grants = ClientCase.objects.filter(
        client=request.user
    ).select_related('case', 'case__assigned_to').order_by('-added_at')

    return render(request, 'cases/client_portal.html', {
        'grants': grants,
        'today':  timezone.now().date(),
    })


@login_required
def client_case_view(request, pk):
    """
    Read-only case view for a specific client.
    Clients can only see what they've been granted access to.
    """
    if request.user.role != 'client':
        return redirect('case_detail', pk=pk)

    grant = get_object_or_404(ClientCase, case_id=pk, client=request.user)
    case  = grant.case

    updates   = case.updates.all().order_by('-date') if grant.can_view_updates   else []
    documents = case.documents.all()                  if grant.can_view_documents else []

    return render(request, 'cases/client_case_view.html', {
        'case': case, 'grant': grant,
        'updates': updates, 'documents': documents,
        'today': timezone.now().date(),
    })


@login_required
def manage_client_access(request, pk):
    """Add / remove client access to a case. Staff only."""
    if request.user.role not in ('admin', 'lawyer'):
        messages.error(request, 'You do not have permission to manage client access.')
        return redirect('case_detail', pk=pk)

    case   = get_object_or_404(Case, pk=pk)
    grants = ClientCase.objects.filter(case=case).select_related('client')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'grant':
            form = ClientGrantForm(request.POST)
            if form.is_valid():
                client_id = form.cleaned_data['client']
                try:
                    from accounts.models import User as UserModel
                    client = UserModel.objects.get(pk=client_id, role='client')
                    ClientCase.objects.get_or_create(
                        client=client, case=case,
                        defaults={
                            'added_by':           request.user,
                            'can_view_documents': form.cleaned_data.get('can_view_documents', True),
                            'can_view_updates':   form.cleaned_data.get('can_view_updates', True),
                        }
                    )
                    # Email the client their portal link
                    _email_client_portal_invite(client, case, request)
                    messages.success(request, f'Access granted to {client.get_full_name() or client.username}.')
                except Exception as e:
                    messages.error(request, f'Could not grant access: {e}')
            else:
                messages.error(request, 'Invalid form data.')

        elif action == 'revoke':
            grant_id = request.POST.get('grant_id')
            ClientCase.objects.filter(pk=grant_id, case=case).delete()
            messages.success(request, 'Client access revoked.')

    form = ClientGrantForm()
    return render(request, 'cases/manage_client_access.html', {
        'case': case, 'grants': grants, 'form': form,
    })


def _email_client_portal_invite(client, case, request):
    """Send the client an email with their portal link."""
    try:
        from django.core.mail import EmailMultiAlternatives
        site = getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000')
        portal_url = f'{site}/client/cases/{case.pk}/'
        login_url  = f'{site}/login/'
        subject = f'Case Update Access — {case.case_number}'
        plain = (
            f'Dear {client.get_full_name() or client.username},\n\n'
            f'You have been granted access to view updates for:\n'
            f'Case: {case.title}\nCase No: {case.case_number}\n\n'
            f'Login at: {login_url}\nYour username: {client.username}\n\n'
            f'View your case: {portal_url}\n\n— {settings.SITE_NAME}'
        )
        html = f"""
        <div style="font-family:sans-serif;max-width:520px;margin:0 auto">
          <div style="background:#0f1b35;padding:22px 26px;border-radius:12px 12px 0 0">
            <h1 style="color:white;font-size:18px;margin:0">⚖ {settings.SITE_NAME}</h1>
          </div>
          <div style="background:white;border:1px solid #e5e7eb;border-top:none;padding:26px;border-radius:0 0 12px 12px">
            <p style="font-size:15px;color:#0f1b35">Dear {client.get_full_name() or client.username},</p>
            <p style="font-size:14px;color:#374151">You have been granted access to view case updates for:</p>
            <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:14px 18px;margin:16px 0">
              <strong style="font-size:15px">{case.title}</strong><br>
              <span style="font-size:13px;color:#6b7280">Case No: {case.case_number} · {case.court_name}</span>
            </div>
            <p style="font-size:14px;color:#374151">Your login: <strong>{client.username}</strong></p>
            <a href="{portal_url}" style="display:inline-block;background:#0f1b35;color:white;padding:11px 22px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600;margin-top:8px">View Case Updates →</a>
          </div>
        </div>"""
        msg = EmailMultiAlternatives(subject, plain,
            settings.DEFAULT_FROM_EMAIL, [client.email])
        msg.attach_alternative(html, 'text/html')
        msg.send()
    except Exception:
        pass   # Non-fatal — don't break the grant flow


# ── AI Case Summary ────────────────────────────────────────────────────────

@login_required
@require_POST
def ai_case_summary(request, pk):
    """
    AJAX: Returns case context JSON for optional client-side prompts.
    Server-side Gemini features use /cases/<pk>/gemini/* endpoints instead.
    """
    case = get_object_or_404(Case, pk=pk)
    return JsonResponse({
        'ok': True,
        'case_context': {
            'case_number': case.case_number,
            'title':       case.title,
            'court':       case.court_name,
            'case_type':   case.get_case_type_display(),
            'parties':     f'{case.petitioner} vs {case.respondent}',
            'status':      case.get_status_display(),
        }
    })


@login_required
@require_POST
def generate_core_summary(request, pk):
    """
    AJAX: Generate a deterministic 'Core Summary (AI Generated)' using only
    case details + recent internal records (no Claude / no external API).
    """
    case = get_object_or_404(Case, pk=pk)
    summary = _build_core_summary(case)
    return JsonResponse({'ok': True, 'summary': summary})


@login_required
@require_POST
def save_ai_summary(request, pk):
    """Save AI-generated summary as a CaseUpdate."""
    case = get_object_or_404(Case, pk=pk)
    try:
        data    = json.loads(request.body)
        summary = data.get('summary', '').strip()
        title   = data.get('title', 'AI Analysis').strip()
        if not summary:
            return JsonResponse({'ok': False, 'error': 'Empty summary'})
        CaseUpdate.objects.create(
            case=case,
            title=f'[AI] {title}',
            description=summary,
            created_by=request.user,
        )
        return JsonResponse({'ok': True, 'message': 'Summary saved as case update.'})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


# ── Gemini-backed AI (server-side, API key from env) ─────────────────────

_HEARING_GEMINI_INSTRUCTIONS = {
    'summary': (
        'Provide a concise professional summary of this hearing. Include what was discussed, '
        'any orders or directions if inferable from notes, parties\' positions, and practical next steps.'
    ),
    'document': (
        'List documents to prepare or file before the next hearing: applications, affidavits, '
        'compilations, indices, written submissions, proofs of service. Use a numbered checklist.'
    ),
    'requirements': (
        'Outline procedural and substantive preparation for the next hearing: filings, compliance, '
        'arguments, court fees, witnesses, logistics.'
    ),
    'arguments': (
        'Draft a structured outline of issues and arguments counsel might advance. '
        'Do not invent statute citations or case law; mark placeholders where law must be supplied.'
    ),
    'risks': (
        'Identify practical risks for the next hearing and mitigation steps. Neutral, non-alarmist tone.'
    ),
}


def _hearing_fallback_text(mode, *, custom, hearing_date, purpose, notes, scraped, judge_name):
    task = custom if mode == 'custom' else _HEARING_GEMINI_INSTRUCTIONS.get(mode, 'Provide hearing assistance.')
    out = [
        f"Task: {task}",
        "",
        f"Hearing date: {hearing_date or '—'}",
        f"Purpose: {purpose or '—'}",
        f"Judge / bench: {judge_name or '—'}",
        "",
        "Notes:",
        notes or "—",
    ]
    if scraped:
        out.extend(["", "Court excerpt:", scraped[:1800]])
    out.extend([
        "",
        "Practical next steps:",
        "1. Verify the latest order/directions and compliance points.",
        "2. Prepare filings/documents required before the next listing.",
        "3. Keep argument notes + document index ready for hearing.",
    ])
    return "\n".join(out).strip()


def _document_draft_fallback(case, template, extra):
    labels = dict(DocumentTemplateForm.TEMPLATE_CHOICES)
    label = labels.get(template, template or 'document draft')
    return (
        f"{label}\n\n"
        f"Court: {case.court_name}\n"
        f"Case No.: {case.case_number}\n"
        f"Title: {case.title}\n"
        f"Parties: {case.petitioner or '[FILL]'} vs {case.respondent or '[FILL]'}\n"
        f"Filing date: {_fmt_date(case.filing_date)}\n"
        f"Next hearing: {_fmt_date(case.next_hearing_date)}\n\n"
        "Draft skeleton:\n"
        "1. Cause title and jurisdiction\n"
        "2. Brief facts and chronology\n"
        "3. Grounds / submissions\n"
        "4. Reliefs prayed for\n"
        "5. Verification / signature block\n\n"
        f"Additional notes: {extra or '—'}\n\n"
        "Note: This is a deterministic fallback draft. Review before filing."
    )


@login_required
@require_POST
def gemini_hearing_assist(request, pk):
    if not gemini_service.is_configured():
        return JsonResponse({'ok': False, 'skipped': True, 'error': 'Gemini not configured'})
    case = get_object_or_404(Case, pk=pk)
    try:
        data = json.loads(request.body or '{}')
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

    mode = (data.get('mode') or '').strip()
    custom = (data.get('custom_question') or '').strip()
    hearing_date = (data.get('hearing_date') or '').strip()
    purpose = (data.get('purpose') or '').strip()
    notes = (data.get('notes') or '').strip()
    scraped = (data.get('scraped') or '').strip()
    judge_name = (data.get('judge_name') or '').strip()

    if mode == 'custom':
        task = custom or 'Answer helpfully using only the hearing context below.'
    else:
        task = _HEARING_GEMINI_INSTRUCTIONS.get(mode)
        if not task:
            return JsonResponse({'ok': False, 'error': 'Unknown mode'}, status=400)

    ctx = (
        f"Case: {case.title} ({case.case_number})\n"
        f"Court: {case.court_name}\n"
        f"Type: {case.get_case_type_display()}\n"
        f"Parties: {case.petitioner or '—'} vs {case.respondent or '—'}\n"
        f"Hearing date: {hearing_date or '—'}\n"
        f"Purpose listed: {purpose or '—'}\n"
        f"Judge / bench: {judge_name or '—'}\n"
        f"Internal notes: {notes or '—'}\n"
        f"Court page excerpt (may be noisy): {(scraped[:6000] + ('…' if len(scraped) > 6000 else '')) if scraped else '—'}"
    )
    sys_inst = (
        "You assist litigation teams using LegalCRM. Output plain text for a case file. "
        "Do not assert legal outcomes; flag uncertainty. Not legal advice."
    )
    user_prompt = f"Task:\n{task}\n\nContext:\n{ctx}"
    try:
        text = gemini_service.generate_text(
            system_instruction=sys_inst,
            user_prompt=user_prompt,
            max_output_tokens=8192,
            temperature=0.35,
        )
        return JsonResponse({'ok': True, 'text': text, 'source': 'gemini'})
    except Exception as e:
        fallback = _hearing_fallback_text(
            mode,
            custom=custom,
            hearing_date=hearing_date,
            purpose=purpose,
            notes=notes,
            scraped=scraped,
            judge_name=judge_name,
        )
        return JsonResponse({
            'ok': True,
            'text': fallback,
            'source': 'fallback',
            'warning': str(e),
        })


@login_required
@require_POST
def gemini_document_draft(request, pk):
    if not gemini_service.is_configured():
        return JsonResponse({'ok': False, 'skipped': True, 'error': 'Gemini not configured'})
    case = get_object_or_404(Case, pk=pk)
    try:
        data = json.loads(request.body or '{}')
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

    template = (data.get('template_type') or '').strip()
    extra = (data.get('additional_notes') or '').strip()
    valid = {c[0] for c in DocumentTemplateForm.TEMPLATE_CHOICES}
    if template not in valid:
        return JsonResponse({'ok': False, 'error': 'Invalid template'}, status=400)

    labels = dict(DocumentTemplateForm.TEMPLATE_CHOICES)
    lawyer = case.assigned_to.get_full_name() if case.assigned_to else ''
    bar = getattr(case.assigned_to, 'bar_number', '') or ''
    brief = (
        f"Court: {case.court_name}\nCase No.: {case.case_number}\nTitle: {case.title}\n"
        f"Parties: {case.petitioner or '[fill]'} vs {case.respondent or '[fill]'}\n"
        f"Filing date: {_fmt_date(case.filing_date)}\nNext hearing: {_fmt_date(case.next_hearing_date)}\n"
        f"Case type: {case.get_case_type_display()}\n"
        f"Counsel: {lawyer or '[fill]'}\nBar number: {bar or '[if applicable]'}\n"
        f"Description from file:\n{(case.description or '')[:1200]}"
    )
    sys_inst = (
        f"Produce a formal {labels.get(template, template)} draft suitable for Indian litigation practice. "
        "Use clear headings and [FILL] placeholders for unknown facts. Plain text. "
        "State that counsel must review before filing. Not legal advice."
    )
    user_prompt = f"{brief}\n\nAdditional instructions from user:\n{extra or 'None.'}"
    try:
        text = gemini_service.generate_text(
            system_instruction=sys_inst,
            user_prompt=user_prompt,
            max_output_tokens=8192,
            temperature=0.25,
        )
        return JsonResponse({'ok': True, 'text': text, 'source': 'gemini'})
    except Exception as e:
        return JsonResponse({
            'ok': True,
            'text': _document_draft_fallback(case, template, extra),
            'source': 'fallback',
            'warning': str(e),
        })


@login_required
@require_POST
def gemini_core_summary(request, pk):
    if not gemini_service.is_configured():
        return JsonResponse({'ok': False, 'skipped': True})
    case = get_object_or_404(Case, pk=pk)
    draft = _build_core_summary(case)
    user_prompt = (
        "Polish the following internal case summary for clarity. Keep the same facts and structure; "
        "do not invent parties, dates, courts, or outcomes. If it notes missing information, preserve that. "
        "Plain text only.\n\n"
        f"{draft[:16000]}"
    )
    sys_inst = "You refine internal litigation summaries in LegalCRM. Factual accuracy over style."
    try:
        text = gemini_service.generate_text(
            system_instruction=sys_inst,
            user_prompt=user_prompt,
            max_output_tokens=8192,
            temperature=0.25,
        )
        return JsonResponse({'ok': True, 'summary': text, 'source': 'gemini'})
    except Exception as e:
        return JsonResponse({
            'ok': True,
            'summary': draft,
            'source': 'fallback',
            'warning': str(e),
        })


# ── Document Generator ────────────────────────────────────────────────────

@login_required
def generate_document(request, pk):
    """
    Generate a court-ready document draft from case data.
    Uses Gemini when GEMINI_API_KEY is set (see gemini_document_draft); otherwise the
    template page offers a deterministic local draft in the browser.
    """
    case = get_object_or_404(Case, pk=pk)
    form = DocumentTemplateForm(request.POST or None)
    return render(request, 'cases/document_generator.html', {
        'case': case, 'form': form,
    })


@login_required
def web_scrapping(request):
    """
    Web scrapping utility page:
    - Fetch HTML/text content from a given court URL
    - Extract PDF links
    - Extract selectable text from PDFs (direct URL or links found on page)
    - Optionally import selected/all PDFs into a case as CaseDocument
    """
    context = {
        'cases': Case.objects.order_by('-updated_at')[:200],
        'target_url': '',
        'pdf_direct_url': '',
        'pdf_fetch_options': None,
        'selected_case_id': '',
        'result': None,
        'standalone_pdf_extraction': None,
        'profiles': SCRAPE_PROFILES,
        'options': {
            'profile': 'auto',
            'follow_embeds': True,
            'verify_ssl': True,
            'timeout_sec': 20,
            'user_agent': '',
            'extract_mode': 'text',
        }
    }

    if request.method != 'POST':
        return render(request, 'cases/web_scrapping.html', context)

    action = (request.POST.get('action') or '').strip()

    if action == 'extract_pdf_direct':
        pdf_direct_url = (request.POST.get('pdf_direct_url') or '').strip()
        verify_ssl = (request.POST.get('verify_ssl_pdf') or '1').strip() in ('1', 'true', 'on', 'yes')
        timeout_sec = int((request.POST.get('timeout_sec_pdf') or '35').strip() or '35')
        timeout_sec = max(10, min(timeout_sec, 120))
        user_agent = (request.POST.get('user_agent_pdf') or '').strip()
        context['pdf_direct_url'] = pdf_direct_url
        context['pdf_fetch_options'] = {
            'timeout_sec': timeout_sec,
            'verify_ssl': verify_ssl,
            'user_agent': user_agent,
        }
        if not pdf_direct_url:
            messages.error(request, 'Paste a direct link to a PDF file.')
            return render(request, 'cases/web_scrapping.html', context)
        fetch_url = pdf_direct_url
        if not re.match(r'^https?://', fetch_url, re.IGNORECASE):
            fetch_url = 'https://' + fetch_url
            context['pdf_direct_url'] = fetch_url
        try:
            raw = _http_fetch_binary(
                fetch_url,
                referer=None,
                user_agent=user_agent or None,
                verify_ssl=verify_ssl,
                timeout_sec=timeout_sec,
            )
            if not _looks_like_pdf_bytes(raw):
                messages.error(
                    request,
                    'That URL did not return a valid PDF. Use a link that downloads/opens a .pdf file directly.',
                )
                return render(request, 'cases/web_scrapping.html', context)
            txt, err = _extract_pdf_text_from_bytes(raw)
            context['standalone_pdf_extraction'] = {
                'url': fetch_url,
                'ok': bool((txt or '').strip()),
                'text': _format_extracted_text(txt, max_chars=50000) if txt else '',
                'error': err or '',
            }
            if (txt or '').strip():
                messages.success(
                    request,
                    'PDF text extracted below. You can copy it or import the file into a case separately.',
                )
            elif err:
                messages.warning(request, err)
        except Exception as e:
            messages.error(request, f'Could not download or read PDF: {e}')
        return render(request, 'cases/web_scrapping.html', context)
    target_url = (request.POST.get('target_url') or '').strip()
    selected_case_id = (request.POST.get('case_id') or '').strip()
    profile = (request.POST.get('profile') or 'auto').strip()
    follow_embeds = _to_bool(request.POST.get('follow_embeds'), default=True)
    verify_ssl = _to_bool(request.POST.get('verify_ssl'), default=True)
    timeout_sec = int((request.POST.get('timeout_sec') or '20').strip() or '20')
    timeout_sec = max(5, min(timeout_sec, 90))
    user_agent = (request.POST.get('user_agent') or '').strip()
    extract_mode = (request.POST.get('extract_mode') or 'text').strip().lower()
    if extract_mode not in ('text', 'ocr', 'both'):
        extract_mode = 'text'

    context['target_url'] = target_url
    context['selected_case_id'] = selected_case_id
    context['options'] = {
        'profile': profile,
        'follow_embeds': follow_embeds,
        'verify_ssl': verify_ssl,
        'timeout_sec': timeout_sec,
        'user_agent': user_agent,
        'extract_mode': extract_mode,
    }

    if not target_url:
        messages.error(request, 'Please enter a website URL to scrape.')
        return render(request, 'cases/web_scrapping.html', context)

    # Ensure URL has scheme
    if not re.match(r'^https?://', target_url, re.IGNORECASE):
        target_url = 'https://' + target_url
        context['target_url'] = target_url

    # Apply profile defaults only where user didn't provide explicit overrides.
    effective_profile_key, effective_profile = _effective_profile(profile, target_url)
    auto_user_agent = user_agent or effective_profile.get('user_agent') or ''
    if request.POST.get('follow_embeds') is None:
        follow_embeds = bool(effective_profile.get('follow_embeds', True))
    if request.POST.get('verify_ssl') is None:
        verify_ssl = bool(effective_profile.get('verify_ssl', True))
    if not request.POST.get('timeout_sec'):
        timeout_sec = int(effective_profile.get('timeout_sec', 20))

    context['options'].update({
        'profile': profile,
        'effective_profile': effective_profile_key,
        'follow_embeds': follow_embeds,
        'verify_ssl': verify_ssl,
        'timeout_sec': timeout_sec,
        'user_agent': auto_user_agent,
        'extract_mode': extract_mode,
    })

    try:
        fetch = _fetch_url_bytes(
            target_url,
            timeout_sec=timeout_sec,
            verify_ssl=verify_ssl,
            headers=_build_request_headers(user_agent=auto_user_agent or None)
        )
        content_type = fetch['content_type']
        raw = fetch['bytes']
        final_url = fetch['final_url']
        body = raw.decode('utf-8', errors='ignore')
        title_match = re.search(r'<title[^>]*>(.*?)</title>', body, re.IGNORECASE | re.DOTALL)
        page_title = re.sub(r'\s+', ' ', (title_match.group(1).strip() if title_match else 'Untitled page'))

        # Multi-strategy link extraction from page
        all_links = _extract_links_from_html(body, final_url)

        # Optional: also follow iframe/object/embed sources once
        if follow_embeds:
            embed_links = []
            for attr in ('src', 'data'):
                for val in re.findall(rf'(?:iframe|embed|object)[^>]+{attr}\s*=\s*["\']([^"\']+)["\']', body, flags=re.IGNORECASE):
                    embed_links.append(urljoin(final_url, unescape(val)))

            for emb in embed_links[:8]:
                try:
                    emb_fetch = _fetch_url_bytes(
                        emb,
                        timeout_sec=timeout_sec,
                        verify_ssl=verify_ssl,
                        headers=_build_request_headers(user_agent=user_agent or None, referer=final_url)
                    )
                    emb_ct = emb_fetch['content_type']
                    emb_body = emb_fetch['bytes'].decode('utf-8', errors='ignore')
                    if 'pdf' in emb_ct or emb.lower().endswith('.pdf'):
                        all_links.append(emb)
                    else:
                        all_links.extend(_extract_links_from_html(emb_body, emb_fetch['final_url']))
                except Exception:
                    continue

        hints = [h.lower() for h in effective_profile.get('pdf_keyword_hints', [])]
        pdf_links = []
        for ln in all_links:
            lo = ln.lower()
            if '.pdf' in lo or 'download' in lo or 'document' in lo or any(h in lo for h in hints):
                pdf_links.append(ln)

        # Deduplicate while preserving order
        seen = set()
        dedup_pdf_links = []
        for link in pdf_links:
            if link in seen:
                continue
            seen.add(link)
            dedup_pdf_links.append(link)

        # Basic text extraction for preview
        extraction_notes = []
        text_preview = ''

        # Standard HTML/text extraction path
        if extract_mode in ('text', 'both'):
            stripped = re.sub(r'<script[\s\S]*?</script>', ' ', body, flags=re.IGNORECASE)
            stripped = re.sub(r'<style[\s\S]*?</style>', ' ', stripped, flags=re.IGNORECASE)
            stripped = re.sub(r'<[^>]+>', ' ', stripped)
            text_preview = re.sub(r'\s+', ' ', stripped).strip()

        # OCR path (mainly useful for image URLs)
        if extract_mode in ('ocr', 'both'):
            if 'image/' in content_type:
                ocr_text, ocr_err = _ocr_image_bytes(raw)
                if ocr_text.strip():
                    joiner = '\n\n--- OCR EXTRACT ---\n\n' if text_preview else ''
                    text_preview = f"{text_preview}{joiner}{ocr_text.strip()}"
                    extraction_notes.append('OCR extraction used on image content.')
                elif ocr_err:
                    extraction_notes.append(ocr_err)
            elif extract_mode == 'ocr':
                extraction_notes.append('OCR works on image responses; this URL returned non-image content.')

        text_preview = _format_extracted_text(text_preview, max_chars=10000)
        extracted = _extract_case_signals(text_preview)

        context['result'] = {
            'page_title': page_title,
            'content_type': content_type or 'unknown',
            'final_url': final_url,
            'pdf_links': dedup_pdf_links,
            'order_rows': [],
            'text_preview': text_preview,
            'formatted_text': text_preview,
            'pdf_count': len(dedup_pdf_links),
            'total_links_found': len(all_links),
            'case_signals': extracted,
            'effective_profile': effective_profile_key,
            'extract_mode': extract_mode,
            'extraction_notes': extraction_notes,
        }

        if 'delhihighcourt.nic.in/app/case-type-status-details/' in final_url.lower():
            try:
                order_rows = _fetch_delhi_high_court_orders(
                    final_url,
                    timeout_sec=max(timeout_sec, 25),
                    verify_ssl=verify_ssl,
                    user_agent=auto_user_agent or None,
                )
                context['result']['order_rows'] = order_rows
                context['result']['pdf_links'] = [r['pdf_url'] for r in order_rows]
                context['result']['pdf_count'] = len(order_rows)
                context['result']['total_links_found'] = max(len(all_links), len(order_rows))
                if order_rows:
                    extraction_notes.append('Delhi High Court order rows loaded from the page AJAX data.')
            except Exception as e:
                extraction_notes.append(f'Delhi High Court order parsing failed: {e}')

        if action == 'extract_pdf_content':
            selected = request.POST.getlist('selected_pdf_links')
            extractions = []
            if not selected:
                messages.warning(
                    request,
                    'Select at least one PDF in the list, then click “Extract PDF text”.',
                )
            else:
                for link in selected[:5]:
                    entry = {'url': link, 'ok': False, 'text': '', 'error': ''}
                    try:
                        raw = _http_fetch_binary(
                            link,
                            referer=final_url,
                            user_agent=auto_user_agent or None,
                            verify_ssl=verify_ssl,
                            timeout_sec=max(timeout_sec, 35),
                        )
                        if not _looks_like_pdf_bytes(raw):
                            entry['error'] = (
                                'Downloaded file is not a valid PDF (use a direct .pdf link).'
                            )
                        else:
                            txt, err = _extract_pdf_text_from_bytes(raw)
                            if (txt or '').strip():
                                entry['ok'] = True
                                entry['text'] = _format_extracted_text(txt, max_chars=50000)
                            else:
                                entry['error'] = err or 'No text extracted.'
                    except Exception as e:
                        entry['error'] = str(e)
                    extractions.append(entry)
                ok_n = sum(1 for e in extractions if e['ok'])
                if ok_n:
                    messages.success(
                        request,
                        f'Extracted text from {ok_n} of {len(extractions)} PDF(s). Scroll to “PDF text extraction”.',
                    )
                elif extractions:
                    messages.warning(
                        request,
                        'No text could be extracted from the selected PDF(s). See per-file errors below.',
                    )
                context['result']['pdf_content_extractions'] = extractions

        if action == 'import_order_rows':
            if not selected_case_id:
                messages.error(request, 'Please select a case before importing order rows.')
                return render(request, 'cases/web_scrapping.html', context)
            case = get_object_or_404(Case, pk=selected_case_id)
            selected_links = set(request.POST.getlist('selected_pdf_links'))
            order_rows = context['result'].get('order_rows') or []
            if not selected_links:
                selected_links = {row['pdf_url'] for row in order_rows}
            imported = 0
            skipped = 0
            for row in order_rows[:100]:
                pdf_url = (row.get('pdf_url') or '').strip()
                if not pdf_url or pdf_url not in selected_links:
                    continue
                date_text = (row.get('order_date') or '').strip()
                parsed_date = None
                for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d'):
                    try:
                        parsed_date = datetime.strptime(date_text, fmt).date()
                        break
                    except Exception:
                        continue
                if not parsed_date:
                    skipped += 1
                    continue
                existing = CaseCourtOrderLink.objects.filter(case=case, pdf_url=pdf_url).first()
                if existing:
                    skipped += 1
                    continue
                judge_name = ''
                try:
                    pdf_bytes = _http_fetch_binary(
                        pdf_url,
                        referer=final_url,
                        user_agent=auto_user_agent or None,
                        verify_ssl=verify_ssl,
                        timeout_sec=max(timeout_sec, 35),
                    )
                    pdf_text, _ = _extract_pdf_text_from_bytes(pdf_bytes, max_pages=5)
                    judge_name = _extract_judge_name_from_text(pdf_text)
                except Exception:
                    judge_name = ''
                row_obj = CaseCourtOrderLink.objects.create(
                    case=case,
                    link_label=(row.get('link_label') or case.case_number)[:200],
                    pdf_url=pdf_url,
                    order_date=parsed_date,
                    judge_name=(judge_name or '')[:200],
                    created_by=request.user,
                )
                log_court_order_link_event(case, row_obj, user=request.user)
                imported += 1
            if imported:
                messages.success(request, f'Imported {imported} order PDF row(s) into the case order register.')
            if skipped:
                messages.info(request, f'Skipped {skipped} row(s) because they already existed or had invalid dates.')
            return render(request, 'cases/web_scrapping.html', context)

        if action != 'import_pdfs':
            return render(request, 'cases/web_scrapping.html', context)

        # Import mode
        if not selected_case_id:
            messages.error(request, 'Please select a case before importing PDFs.')
            return render(request, 'cases/web_scrapping.html', context)

        case = get_object_or_404(Case, pk=selected_case_id)
        selected_links = request.POST.getlist('selected_pdf_links')
        if not selected_links:
            selected_links = dedup_pdf_links

        if not selected_links:
            messages.warning(request, 'No PDF links found to import.')
            return render(request, 'cases/web_scrapping.html', context)

        imported = 0
        failed = 0
        for link in selected_links[:20]:  # safety cap per request
            try:
                lreq = urllib.request.Request(
                    link,
                    headers=_build_request_headers(user_agent=auto_user_agent or None, referer=final_url)
                )
                ssl_ctx = None if verify_ssl else ssl._create_unverified_context()
                with urllib.request.urlopen(lreq, timeout=max(timeout_sec, 25), context=ssl_ctx) as lresp:
                    pdf_content_type = (lresp.headers.get('Content-Type') or '').lower()
                    pdf_bytes = lresp.read()

                # Basic validation: either content-type or URL indicates PDF
                if 'pdf' not in pdf_content_type and '.pdf' not in link.lower():
                    failed += 1
                    continue

                parsed = urlparse(link)
                base_name = os.path.basename(parsed.path) or f'court_doc_{uuid.uuid4().hex[:8]}.pdf'
                if not base_name.lower().endswith('.pdf'):
                    base_name += '.pdf'

                # Keep file names filesystem-safe
                safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', base_name)
                doc = CaseDocument(
                    case=case,
                    title=f'Scraped: {safe_name}',
                    document_type='order',
                    uploaded_by=request.user,
                    notes=f'Imported from: {link}',
                )
                doc.file.save(safe_name, ContentFile(pdf_bytes), save=True)
                imported += 1
            except Exception:
                failed += 1

        if imported:
            messages.success(request, f'Imported {imported} PDF(s) to case {case.case_number}.')
        if failed:
            messages.warning(request, f'{failed} PDF link(s) could not be imported.')

        return render(request, 'cases/web_scrapping.html', context)

    except urllib.error.URLError as e:
        messages.error(request, f'Unable to fetch URL: {e}')
    except Exception as e:
        messages.error(request, f'Scrapping failed: {e}')

    return render(request, 'cases/web_scrapping.html', context)


# ═══════════════════════════════════════════════════════════════════════════
# HEARING RECORDS (Change 3)
# ═══════════════════════════════════════════════════════════════════════════

from .models  import HearingRecord, HearingAlertOptOut, CaseCourtOrderLink
from .forms   import HearingRecordForm, AlertOptOutForm


@login_required
def add_court_order_link(request, pk):
    """Add a PDF order URL row (High Court–style register)."""
    case = get_object_or_404(Case, pk=pk)
    if request.method != 'POST':
        return redirect('case_detail', pk=pk)
    form = CaseCourtOrderLinkForm(request.POST)
    if form.is_valid():
        row = form.save(commit=False)
        row.case = case
        row.created_by = request.user
        row.save()
        log_court_order_link_event(case, row, user=request.user)
        messages.success(request, 'Order PDF link added.')
    else:
        messages.error(request, 'Please fix the form errors for the order PDF link.')
    return redirect(f'/cases/{pk}/?sub=orders#casehistory')


@login_required
@require_POST
def import_court_orders_from_url(request, pk):
    """
    Import court order PDF rows into the order register from supported sources.
    Currently supports Delhi High Court (DataTables AJAX-backed list).
    """
    case = get_object_or_404(Case, pk=pk)
    source_url = (request.POST.get('source_url') or '').strip()
    parent_url = (request.POST.get('parent_url') or '').strip()
    pasted_html = (request.POST.get('pasted_html') or '').strip()
    use_pasted_html = bool(pasted_html)

    if not source_url and not use_pasted_html:
        messages.error(request, 'Paste a source URL or paste the listing table HTML snippet.')
        return redirect(f'/cases/{pk}/?sub=orders#casehistory')

    if source_url and not re.match(r'^https?://', source_url, re.IGNORECASE):
        source_url = 'https://' + source_url
    if parent_url and not re.match(r'^https?://', parent_url, re.IGNORECASE):
        parent_url = 'https://' + parent_url

    verify_ssl = _to_bool(request.POST.get('verify_ssl'), default=True)
    create_timeline = _to_bool(request.POST.get('create_timeline'), default=True)
    timeout_sec = int((request.POST.get('timeout_sec') or '35').strip() or '35')
    timeout_sec = max(10, min(timeout_sec, 120))

    lower = source_url.lower() if source_url else ''

    try:
        base_for_relative = parent_url or source_url
        if use_pasted_html:
            rows = _extract_order_rows_from_html_snippet(pasted_html, base_url=base_for_relative)
        elif 'delhihighcourt.nic.in/app/case-type-status-details/' in lower:
            rows = _fetch_delhi_high_court_orders(
                source_url,
                timeout_sec=timeout_sec,
                verify_ssl=verify_ssl,
                user_agent=None,
            )
        else:
            rows = _fetch_generic_listing_history_orders(
                source_url,
                timeout_sec=timeout_sec,
                verify_ssl=verify_ssl,
                user_agent=None,
            )
    except Exception as e:
        messages.error(request, f'Could not load order list: {e}')
        return redirect(f'/cases/{pk}/?sub=orders#casehistory')

    if not rows:
        messages.warning(request, 'No order rows were detected. Paste full <table> HTML (including <tr>/<td>/<a href>) or provide a source URL.')
        return redirect(f'/cases/{pk}/?sub=orders#casehistory')

    imported = 0
    skipped = 0
    judge_filled = 0
    timeline_created = 0
    timeline_skipped = 0
    for row in rows[:150]:
        pdf_url_raw = (row.get('pdf_url') or '').strip()
        pdf_url = urljoin(parent_url or source_url, pdf_url_raw) if (parent_url and pdf_url_raw) else pdf_url_raw
        date_text = (row.get('order_date') or '').strip()
        link_label = (row.get('link_label') or case.case_number).strip()
        if not pdf_url or not date_text:
            skipped += 1
            continue

        parsed_date = None
        for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d'):
            try:
                parsed_date = datetime.strptime(date_text, fmt).date()
                break
            except Exception:
                continue
        if not parsed_date:
            skipped += 1
            continue

        judge_name = ''
        timeline_title = ''
        timeline_points = []
        timeline_dt = None
        timeline_dt_txt = ''
        next_listing_dt = None
        next_listing_txt = ''
        try:
            pdf_bytes = _http_fetch_binary(
                pdf_url,
                referer=source_url,
                user_agent=None,
                verify_ssl=verify_ssl,
                timeout_sec=timeout_sec,
            )
            pdf_text, _ = _extract_pdf_text_from_bytes(pdf_bytes, max_pages=5)
            judge_name = _extract_judge_name_from_text(pdf_text)
            timeline_title = _extract_coram_title(pdf_text)
            timeline_points = _extract_order_numbered_points(pdf_text, limit=8)
            if not timeline_points:
                timeline_points = _extract_key_points_after_coram(pdf_text, limit=8)
            timeline_dt, timeline_dt_txt = _extract_order_date_from_text(pdf_text)
            next_listing_dt, next_listing_txt = _extract_next_listing_date(pdf_text)
        except Exception:
            judge_name = ''

        existing_row = CaseCourtOrderLink.objects.filter(case=case, pdf_url=pdf_url).first()
        if existing_row:
            row_obj = existing_row
            skipped += 1
            # Fill judge name if missing on existing row.
            if judge_name and not (existing_row.judge_name or '').strip():
                existing_row.judge_name = judge_name[:200]
                existing_row.save(update_fields=['judge_name'])
        else:
            row_obj = CaseCourtOrderLink.objects.create(
                case=case,
                link_label=link_label[:200],
                pdf_url=pdf_url,
                order_date=parsed_date,
                judge_name=(judge_name or '')[:200],
                created_by=request.user,
            )
            log_court_order_link_event(case, row_obj, user=request.user)
            imported += 1
        if judge_name:
            judge_filled += 1
        if create_timeline:
            title = (timeline_title or f'Order: {link_label}')[:200]
            event_date = timeline_dt or parsed_date
            desc_lines = []
            if timeline_points:
                desc_lines.extend(timeline_points)
            else:
                desc_lines.append('1. Order imported from source PDF link.')
            if timeline_dt_txt:
                desc_lines.append('')
                desc_lines.append(f"Order date: {timeline_dt_txt}")
            if next_listing_txt:
                desc_lines.append(f"Next listing date: {next_listing_txt}")
            desc_text = '\n'.join(desc_lines).strip()
            # Avoid duplicate timeline rows for the same imported order URL.
            exists_timeline = CaseUpdate.objects.filter(
                case=case,
                date=event_date,
                title=title,
                description__icontains=pdf_url[:180],
            ).exists()
            if exists_timeline:
                timeline_skipped += 1
            else:
                CaseUpdate.objects.create(
                    case=case,
                    date=event_date,
                    title=title,
                    description=f"{desc_text}\n\nSource PDF: {pdf_url}",
                    next_action_date=next_listing_dt,
                    created_by=request.user,
                )
                timeline_created += 1

    if imported:
        messages.success(request, f'Imported {imported} order PDF(s). Judge auto-filled for {judge_filled}.')
    if timeline_created:
        messages.success(request, f'Created {timeline_created} timeline update(s) from imported orders.')
    elif create_timeline:
        messages.info(
            request,
            f'No new timeline rows created (already present: {timeline_skipped}).',
        )
    if skipped:
        messages.info(request, f'Skipped {skipped} row(s) (duplicates or invalid dates).')
    _sync_case_hearing_dates_from_activity(case)
    return redirect(f'/cases/{pk}/?sub=orders#casehistory')


@login_required
@require_POST
def import_court_order_from_upload(request, pk):
    """
    Upload a local order PDF, store it as a case document, add it to the order
    register, and optionally create a timeline update from extracted content.
    """
    case = get_object_or_404(Case, pk=pk)
    uploaded_pdf = request.FILES.get('uploaded_pdf')
    if not uploaded_pdf:
        messages.error(request, 'Please choose a PDF file to upload.')
        return redirect(f'/cases/{pk}/?sub=orders#casehistory')

    filename = (uploaded_pdf.name or '').strip()
    if not filename.lower().endswith('.pdf'):
        messages.error(request, 'Only PDF files are supported here.')
        return redirect(f'/cases/{pk}/?sub=orders#casehistory')

    pdf_bytes = b''
    for chunk in uploaded_pdf.chunks():
        pdf_bytes += chunk
    if not _looks_like_pdf_bytes(pdf_bytes):
        messages.error(request, 'Uploaded file does not appear to be a valid PDF.')
        return redirect(f'/cases/{pk}/?sub=orders#casehistory')

    try:
        uploaded_doc = CaseDocument.objects.create(
            case=case,
            title=(request.POST.get('link_label') or '').strip()[:200] or filename[:200],
            document_type='order',
            file=ContentFile(pdf_bytes, name=filename),
            uploaded_by=request.user,
            notes='Uploaded from Order Register (Case History tab).',
        )
        log_document_event(case, uploaded_doc, 'uploaded', user=request.user)
    except Exception as e:
        messages.error(request, f'Could not store uploaded PDF: {e}')
        return redirect(f'/cases/{pk}/?sub=orders#casehistory')

    pdf_url = request.build_absolute_uri(uploaded_doc.file.url)
    create_timeline = _to_bool(request.POST.get('create_timeline'), default=True)

    extracted_text, extract_err = _extract_pdf_text_from_bytes(pdf_bytes, max_pages=5)
    judge_name = ''
    timeline_title = ''
    timeline_points = []
    timeline_dt = None
    timeline_dt_txt = ''
    next_listing_dt = None
    next_listing_txt = ''
    if extracted_text:
        judge_name = _extract_judge_name_from_text(extracted_text)
        timeline_title = _extract_coram_title(extracted_text)
        timeline_points = _extract_order_numbered_points(extracted_text, limit=8)
        if not timeline_points:
            timeline_points = _extract_key_points_after_coram(extracted_text, limit=8)
        timeline_dt, timeline_dt_txt = _extract_order_date_from_text(extracted_text)
        next_listing_dt, next_listing_txt = _extract_next_listing_date(extracted_text)

    fallback_order_date = _parse_legal_date((request.POST.get('order_date') or '').strip())
    order_date = timeline_dt or fallback_order_date or timezone.now().date()
    link_label = (request.POST.get('link_label') or '').strip() or case.case_number

    row_obj = CaseCourtOrderLink.objects.create(
        case=case,
        link_label=link_label[:200],
        pdf_url=pdf_url,
        order_date=order_date,
        judge_name=(judge_name or '')[:200],
        created_by=request.user,
    )
    log_court_order_link_event(case, row_obj, user=request.user)

    timeline_created = 0
    if create_timeline:
        title = (timeline_title or f'Order: {link_label}')[:200]
        event_date = timeline_dt or order_date
        desc_lines = []
        if timeline_points:
            desc_lines.extend(timeline_points)
        else:
            desc_lines.append('1. Order uploaded and imported into register.')
        if timeline_dt_txt:
            desc_lines.append('')
            desc_lines.append(f'Order date: {timeline_dt_txt}')
        if next_listing_txt:
            desc_lines.append(f'Next listing date: {next_listing_txt}')
        desc_lines.extend([
            '',
            f'Source PDF: {pdf_url}',
            f'Source file: {filename}',
        ])
        desc_text = '\n'.join(desc_lines).strip()
        exists_timeline = CaseUpdate.objects.filter(
            case=case,
            date=event_date,
            title=title,
            description__icontains=pdf_url[:180],
        ).exists()
        if not exists_timeline:
            CaseUpdate.objects.create(
                case=case,
                date=event_date,
                title=title,
                description=desc_text,
                next_action_date=next_listing_dt,
                created_by=request.user,
            )
            timeline_created = 1

    _sync_case_hearing_dates_from_activity(case)
    if extract_err:
        messages.warning(request, f'PDF uploaded, but extraction was partial: {extract_err}')
    messages.success(request, 'Uploaded PDF imported into order register.')
    if timeline_created:
        messages.success(request, 'Timeline update created from uploaded order PDF.')
    elif create_timeline:
        messages.info(request, 'Timeline row already exists for this uploaded order.')
    return redirect(f'/cases/{pk}/?sub=orders#casehistory')


@login_required
@require_POST
def delete_court_order_link(request, pk, link_pk):
    row = get_object_or_404(CaseCourtOrderLink, pk=link_pk, case_id=pk)
    row.delete()
    messages.success(request, 'Order PDF link removed.')
    return redirect(f'/cases/{pk}/?sub=orders#casehistory')


@login_required
def add_hearing_record(request, pk):
    case = get_object_or_404(Case, pk=pk)
    if request.method == 'POST':
        form = HearingRecordForm(request.POST)
        if form.is_valid():
            rec = form.save(commit=False)
            rec.case = case
            rec.created_by = request.user
            rec.save()
            log_hearing_record_event(case, rec, user=request.user)
            _sync_case_hearing_dates_from_activity(case)
            messages.success(request, 'Hearing record added.')
        else:
            messages.error(request, 'Please fix the form errors.')
    return redirect(f'/cases/{pk}/?sub=hearings#casehistory')


@login_required
@require_POST
def delete_hearing_record(request, pk, rec_pk):
    rec = get_object_or_404(HearingRecord, pk=rec_pk, case_id=pk)
    case = rec.case
    rec.delete()
    _sync_case_hearing_dates_from_activity(case)
    messages.success(request, 'Hearing record deleted.')
    return redirect(f'/cases/{pk}/?sub=hearings#casehistory')


@login_required
@require_POST
def scrape_hearing_record(request, pk, rec_pk):
    """Fetch text from a court URL and store as scraped_data on the record."""
    rec = get_object_or_404(HearingRecord, pk=rec_pk, case_id=pk)
    if not rec.scrape_url:
        return JsonResponse({'ok': False, 'error': 'No URL set on this record.'})
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            rec.scrape_url,
            headers={'User-Agent': 'Mozilla/5.0 LegalCRM/1.0'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode('utf-8', errors='ignore')
        # Strip HTML tags simply
        import re
        text = re.sub(r'<[^>]+>', ' ', raw)
        text = re.sub(r'\s+', ' ', text).strip()[:3000]
        rec.scraped_data = text
        rec.save(update_fields=['scraped_data'])
        return JsonResponse({'ok': True, 'text': text})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
@require_POST
def extract_pdf_to_timeline(request, pk):
    """
    Extract text from a PDF source and optionally create timeline updates
    plus a summary update in this case.
    """
    case = get_object_or_404(Case, pk=pk)
    raw_body = request.body
    if not raw_body:
        raw_body = b'{}'
    try:
        data = json.loads(raw_body)
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'}, status=400)

    source_type = (data.get('source_type') or 'document').strip()
    document_id = data.get('document_id')
    pdf_url = (data.get('pdf_url') or '').strip()
    include_summary = bool(data.get('include_summary'))
    include_timeline = bool(data.get('include_timeline', True))

    if document_id is not None and document_id != '':
        try:
            document_id = int(document_id)
        except (TypeError, ValueError):
            document_id = None

    pdf_bytes = b''
    source_label = ''
    if source_type == 'document':
        if not document_id:
            return JsonResponse({'ok': False, 'error': 'Select a PDF document.'}, status=400)
        doc = get_object_or_404(CaseDocument, pk=document_id, case=case)
        if not doc.is_pdf:
            return JsonResponse({'ok': False, 'error': 'Selected document is not a PDF.'}, status=400)
        try:
            with open(doc.file.path, 'rb') as fh:
                pdf_bytes = fh.read()
            source_label = doc.filename
        except Exception as e:
            return JsonResponse({'ok': False, 'error': f'Cannot read selected document: {e}'}, status=400)
    elif source_type == 'url':
        if not pdf_url:
            return JsonResponse({'ok': False, 'error': 'Enter a PDF URL.'}, status=400)
        try:
            req = urllib.request.Request(pdf_url, headers=_build_request_headers())
            with urllib.request.urlopen(req, timeout=30) as resp:
                pdf_bytes = resp.read()
            ct = (resp.headers.get('Content-Type') or '').lower()
            if pdf_bytes[:4] != b'%PDF' and 'pdf' not in ct:
                return JsonResponse({
                    'ok': False,
                    'error': (
                        'URL did not return a PDF (check that the link is a direct .pdf file).'
                    ),
                }, status=400)
            source_label = pdf_url
        except Exception as e:
            return JsonResponse({'ok': False, 'error': f'Unable to fetch PDF URL: {e}'}, status=400)
    else:
        return JsonResponse({'ok': False, 'error': 'Unknown source type.'}, status=400)

    extracted_text, extract_error = _extract_pdf_text_from_bytes(pdf_bytes)
    if not extracted_text:
        return JsonResponse({
            'ok': False,
            'error': extract_error or 'Unable to extract text from PDF.',
        }, status=400)

    formatted_text = _format_extracted_text(extracted_text, max_chars=12000)
    timeline_points = _extract_timeline_points_from_text(formatted_text, limit=8)
    created_updates = 0

    if include_timeline and timeline_points:
        for idx, item in enumerate(timeline_points, start=1):
            event_date = timezone.now().date()
            date_text = item.get('date_text') or ''
            parsed = None
            for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%d/%m/%y', '%d-%m-%y', '%d %b %Y', '%d %B %Y'):
                try:
                    parsed = datetime.strptime(date_text, fmt).date()
                    break
                except Exception:
                    continue
            if parsed:
                event_date = parsed
            CaseUpdate.objects.create(
                case=case,
                date=event_date,
                title=f'[PDF Timeline] Event {idx}',
                description=f"Date: {date_text}\nEvent: {item['description']}",
                created_by=request.user,
            )
            created_updates += 1

    summary_text = ''
    if include_summary:
        first_lines = [ln for ln in formatted_text.splitlines() if ln.strip()][:12]
        summary_text = '\n'.join(first_lines)[:1800]
        if gemini_service.is_configured():
            try:
                summary_text = gemini_service.generate_text(
                    system_instruction=(
                        'Summarize litigation PDF content in plain text with headings: '
                        'Case Context, Key Timeline, Key Directions/Orders, Next Steps.'
                    ),
                    user_prompt=f'Summarize this extracted PDF text:\n\n{formatted_text[:14000]}',
                    max_output_tokens=2048,
                    temperature=0.25,
                )
            except Exception:
                pass

        CaseUpdate.objects.create(
            case=case,
            date=timezone.now().date(),
            title='[PDF Summary] Extracted summary',
            description=summary_text,
            created_by=request.user,
        )
        created_updates += 1

    if created_updates:
        _sync_case_hearing_dates_from_activity(case)

    return JsonResponse({
        'ok': True,
        'source': source_label,
        'formatted_text': formatted_text[:3000],
        'timeline_points': timeline_points,
        'created_updates': created_updates,
        'summary_created': bool(include_summary),
    })


# ═══════════════════════════════════════════════════════════════════════════
# ALERT OPT-OUT (Change 4)
# ═══════════════════════════════════════════════════════════════════════════

def alert_stop_view(request, token):
    """
    Public view (no login) — shown when recipient clicks Stop Alerts.
    Displays a form with reason options, then deactivates the 7-day reminders.
    They still receive the final 1-day-before alert.
    """
    opt_out = get_object_or_404(HearingAlertOptOut, token=token)
    already_done = opt_out.opted_out_at is not None

    if request.method == 'POST' and not already_done:
        form = AlertOptOutForm(request.POST)
        if form.is_valid():
            opt_out.reason      = form.cleaned_data['reason']
            opt_out.custom_note = form.cleaned_data.get('custom_note', '')
            opt_out.opted_out_at = timezone.now()
            opt_out.is_active   = True
            opt_out.save()
            return render(request, 'cases/alert_stopped.html', {'opt_out': opt_out})
    else:
        form = AlertOptOutForm()

    return render(request, 'cases/alert_stop.html', {
        'opt_out':      opt_out,
        'form':         form,
        'already_done': already_done,
    })


# ═══════════════════════════════════════════════════════════════════════════
# LEGI — AI CHATBOT (Change 5)
# ═══════════════════════════════════════════════════════════════════════════

@login_required
@require_POST
def legi_chat(request):
    """
    Legi chatbot: fast local keyword routing first; optional Google Gemini when
    GEMINI_API_KEY is set (fallback replies, or `/ai …` for direct Gemini).
    """
    try:
        data    = json.loads(request.body)
        message = data.get('message', '').strip()
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid request'})

    if not message:
        return JsonResponse({'ok': False, 'error': 'Empty message'})

    raw_message = message
    ml = message.lower()
    if ml.startswith('/ai ') or ml == '/ai':
        rest = message[4:].strip() if ml.startswith('/ai ') else ''
        if not rest:
            return JsonResponse({
                'ok': True,
                'type': 'text',
                'message': (
                    'Add your question after `/ai`, for example:\n'
                    '`/ai What should I double-check before hearings this week?`'
                ),
            })
        if not gemini_service.is_configured():
            return JsonResponse(
                {'ok': False, 'error': 'Gemini is not configured. Set GEMINI_API_KEY on the server.'},
                status=503,
            )
        try:
            return JsonResponse({'ok': True, 'type': 'text', 'message': _legi_gemini_reply(request.user, rest)})
        except Exception as e:
            fallback = (
                "Gemini is temporarily unavailable (quota or API issue). "
                "You can still use Legi for CRM navigation and case data queries.\n\n"
                f"Temporary error: {e}"
            )
            return JsonResponse({'ok': True, 'type': 'text', 'message': fallback})

    if ml.startswith('ai:'):
        rest = message.split(':', 1)[1].strip()
        if not rest:
            return JsonResponse({
                'ok': True,
                'type': 'text',
                'message': 'Use `ai: your question` to talk to Gemini with your CRM snapshot.',
            })
        if not gemini_service.is_configured():
            return JsonResponse(
                {'ok': False, 'error': 'Gemini is not configured. Set GEMINI_API_KEY on the server.'},
                status=503,
            )
        try:
            return JsonResponse({'ok': True, 'type': 'text', 'message': _legi_gemini_reply(request.user, rest)})
        except Exception as e:
            fallback = (
                "Gemini is temporarily unavailable (quota or API issue). "
                "You can still use Legi for CRM navigation and case data queries.\n\n"
                f"Temporary error: {e}"
            )
            return JsonResponse({'ok': True, 'type': 'text', 'message': fallback})

    # ── Local intent resolution (instant, no API call) ────────────────────
    message = raw_message
    msg_lower = message.lower()

    # Navigation intents (enhanced keyword coverage)
    nav_intents = [
        (['dashboard', 'home', 'main page', 'start page'], '/dashboard/', 'Taking you to the Dashboard'),
        (['all case', 'case list', 'show cases', 'matter list', 'all matters'], '/cases/', 'Opening All Cases'),
        (['workload', 'lawyer load', 'team load', 'allocation'], '/workload/', 'Opening Workload Dashboard'),
        (['analytics', 'report', 'win rate', 'stats', 'insights'], '/analytics/', 'Opening Analytics Report'),
        (['notification', 'alerts', 'reminders'], '/notifications/', 'Opening Notifications'),
        (['add case', 'new case', 'create case', 'register case'], '/cases/add/', 'Opening Add New Case form'),
        (['user', 'team member', 'manage user', 'staff'], '/users/', 'Opening User Management'),
        (['add user', 'new user', 'create user'], '/users/add/', 'Opening Add User form'),
        (['profile', 'my profile', 'account'], '/profile/', 'Opening your profile'),
        (['web scrap', 'web scrapping', 'scrape', 'court website'], '/web-scrapping/', 'Opening Web Scrapping'),
        (['client portal', 'client page'], '/client/', 'Opening Client Portal'),
    ]
    for keywords, url, reply in nav_intents:
        if any(k in msg_lower for k in keywords):
            return JsonResponse({'ok': True, 'type': 'navigate',
                                 'url': url, 'message': reply})

    import re
    # Case lookup by case number (supports broader formats)
    cn_match = re.search(r'[A-Z]{1,}[A-Z0-9()./\-\s]{2,}\d{1,5}/\d{4}', message, re.IGNORECASE)
    if cn_match:
        try:
            raw_case_no = re.sub(r'\s+', ' ', cn_match.group()).strip()
            case = Case.objects.filter(case_number__iexact=raw_case_no).first()
            if not case:
                case = Case.objects.filter(case_number__icontains=raw_case_no).order_by('-updated_at').first()
            if not case:
                raise Case.DoesNotExist
            date_bits = [
                f"Filing: {_fmt_date(case.filing_date)}",
                f"Last hearing: {_fmt_date(case.last_hearing_date)}",
                f"Next hearing: {_fmt_date(case.next_hearing_date)}",
            ]
            details = (
                f"Found case {case.case_number}\n"
                f"Title: {case.title}\n"
                f"Court: {case.court_name}\n"
                f"Status: {case.get_status_display()}\n"
                f"Parties: {case.petitioner or '—'} vs {case.respondent or '—'}\n"
                + "\n".join(date_bits)
            )
            return JsonResponse({
                'ok': True, 'type': 'navigate',
                'url': f'/cases/{case.pk}/',
                'message': details
            })
        except Case.DoesNotExist:
            pass

    # Search by petitioner/respondent/title/advocate/project/court/case no
    search_triggers = ['find', 'search', 'show', 'look for', 'open', 'case of', 'cases for']
    def _extract_search_term(msg_text):
        term = msg_text.lower()
        for trigger in search_triggers:
            if trigger in term:
                term = term.split(trigger, 1)[1]
                break
        for stop in [
            'case', 'cases', 'matter', 'matters', 'details', 'detail', 'about',
            'named', 'called', 'related to', 'regarding', 'please', 'show', 'open',
            'tell me', 'find', 'search', 'for', 'of', 'the', 'and'
        ]:
            term = term.replace(stop, ' ')
        term = re.sub(r'\s+', ' ', term).strip().strip('"\'')
        return term

    if any(t in msg_lower for t in search_triggers):
        # Extract search term — everything after the trigger word
        term = _extract_search_term(msg_lower)
        if term:
            return JsonResponse({
                'ok': True, 'type': 'navigate',
                'url': f'/cases/?search={term}',
                'message': f'Searching cases for "{term}"'
            })

    # ── Aggregate stats / workload keywords ─────────────────────────────────
    today = timezone.now().date()
    week_end = today + timezone.timedelta(days=7)
    month_end = today + timezone.timedelta(days=30)
    upcoming = Case.objects.filter(
        next_hearing_date__gte=today,
        next_hearing_date__lte=week_end
    ).exclude(status='disposed').count()
    total = Case.objects.count()
    inprog = Case.objects.filter(status='in_progress').count()
    disposed = Case.objects.filter(status='disposed').count()
    on_hold = Case.objects.filter(status='on_hold').count()
    overdue_deadlines = CaseDeadline.objects.filter(due_date__lt=today, is_completed=False).count()
    pending_tasks = CaseTask.objects.filter(is_completed=False).count()
    unread_notifs = Notification.objects.filter(user=request.user, is_read=False).count()
    hearings_month = Case.objects.filter(
        next_hearing_date__gte=today, next_hearing_date__lte=month_end
    ).exclude(status='disposed').count()

    week_from = today.strftime('%Y-%m-%d')
    week_to = week_end.strftime('%Y-%m-%d')
    month_from = today.strftime('%Y-%m-%d')
    month_to = month_end.strftime('%Y-%m-%d')

    if any(k in msg_lower for k in ['total cases', 'how many cases', 'case count']):
        return JsonResponse({
            'ok': True, 'type': 'text',
            'message': f"Total cases: {total}\nIn progress (pending): {inprog}\nDisposed: {disposed}\nOn hold: {on_hold}"
        })
    if any(k in msg_lower for k in ['pending cases', 'in progress cases', 'open cases']):
        return JsonResponse({'ok': True, 'type': 'text', 'message': f"Pending / In progress cases: {inprog}"})
    if any(k in msg_lower for k in ['disposed cases', 'closed cases']):
        return JsonResponse({'ok': True, 'type': 'text', 'message': f"Disposed cases: {disposed}"})
    if (
        'hearing' in msg_lower and
        any(k in msg_lower for k in ['week', '7 day', '7 days', 'next 7', 'upcoming 7'])
    ):
        return JsonResponse({
            'ok': True,
            'type': 'navigate',
            'url': f'/cases/?hearing_from={week_from}&hearing_to={week_to}',
            'message': (
                f"Upcoming hearings in next 7 days: {upcoming}\n"
                "Click below to open the filtered case list."
            )
        })
    if (
        'hearing' in msg_lower and
        any(k in msg_lower for k in ['month', '30 day', '30 days', 'next 30', 'upcoming 30'])
    ):
        return JsonResponse({
            'ok': True,
            'type': 'navigate',
            'url': f'/cases/?hearing_from={month_from}&hearing_to={month_to}',
            'message': (
                f"Upcoming hearings in next 30 days: {hearings_month}\n"
                "Click below to open the filtered case list."
            )
        })
    if 'overdue' in msg_lower and 'deadline' in msg_lower:
        return JsonResponse({'ok': True, 'type': 'text', 'message': f"Overdue deadlines: {overdue_deadlines}"})
    if ('pending' in msg_lower or 'open' in msg_lower) and 'task' in msg_lower:
        return JsonResponse({'ok': True, 'type': 'text', 'message': f"Pending tasks: {pending_tasks}"})
    if 'notification' in msg_lower or 'alerts' in msg_lower:
        return JsonResponse({'ok': True, 'type': 'text', 'message': f"Unread notifications for you: {unread_notifs}"})

    # ── Keyword-based case data answering ────────────────────────────────────
    # If query references a likely case attribute, find top matching cases.
    field_keywords = [
        'date', 'hearing', 'filing', 'title', 'status', 'court', 'petitioner',
        'respondent', 'party', 'project', 'lawyer', 'assigned', 'case number'
    ]
    if any(k in msg_lower for k in field_keywords):
        # Create a crude search term by removing known filler words.
        scrub = _extract_search_term(msg_lower) or re.sub(r'\s+', ' ', msg_lower).strip()

        # Pull candidates by broad matching, then format requested fields.
        candidates = Case.objects.none()
        if scrub:
            candidates = Case.objects.filter(
                Q(case_number__icontains=scrub) |
                Q(title__icontains=scrub) |
                Q(court_name__icontains=scrub) |
                Q(petitioner__icontains=scrub) |
                Q(respondent__icontains=scrub) |
                Q(project__icontains=scrub) |
                Q(assigned_to__first_name__icontains=scrub) |
                Q(assigned_to__last_name__icontains=scrub) |
                Q(assigned_to__username__icontains=scrub)
            ).select_related('assigned_to')[:5]

        # If no specific scrub match, return recent snapshot.
        if not candidates:
            candidates = Case.objects.select_related('assigned_to').order_by('-updated_at')[:5]

        want_dates = any(k in msg_lower for k in ['date', 'hearing', 'filing'])
        want_title = 'title' in msg_lower
        want_status = 'status' in msg_lower
        want_court = 'court' in msg_lower
        want_parties = any(k in msg_lower for k in ['petitioner', 'respondent', 'party'])
        want_lawyer = any(k in msg_lower for k in ['lawyer', 'assigned'])
        want_project = 'project' in msg_lower
        want_case_no = 'case number' in msg_lower or 'number' in msg_lower

        # If user did not ask for one specific field, provide complete compact row.
        specific = any([want_dates, want_title, want_status, want_court, want_parties, want_lawyer, want_project, want_case_no])
        lines = []
        for c in candidates:
            if not specific:
                lines.append(
                    f"{c.case_number} | {c.title} | {c.get_status_display()} | "
                    f"Next hearing: {_fmt_date(c.next_hearing_date)}"
                )
                continue

            parts = []
            if want_case_no:
                parts.append(f"Case No: {c.case_number}")
            if want_title:
                parts.append(f"Title: {c.title}")
            if want_status:
                parts.append(f"Status: {c.get_status_display()}")
            if want_court:
                parts.append(f"Court: {c.court_name}")
            if want_parties:
                parts.append(f"Parties: {c.petitioner or '—'} vs {c.respondent or '—'}")
            if want_lawyer:
                assigned = c.assigned_to.get_full_name() if c.assigned_to else 'Unassigned'
                parts.append(f"Assigned lawyer: {assigned}")
            if want_project:
                parts.append(f"Project: {c.project or '—'}")
            if want_dates:
                parts.append(
                    f"Dates: Filing {_fmt_date(c.filing_date)}, Last hearing {_fmt_date(c.last_hearing_date)}, Next hearing {_fmt_date(c.next_hearing_date)}"
                )
            lines.append(" | ".join(parts))

        return JsonResponse({
            'ok': True,
            'type': 'text',
            'message': "Matching case info:\n" + "\n".join([f"- {ln}" for ln in lines])
        })

    # Generic case keyword fallback (works even without explicit trigger words)
    generic_term = _extract_search_term(msg_lower)
    if generic_term and len(generic_term) >= 3:
        has_case_hits = Case.objects.filter(
            Q(case_number__icontains=generic_term) |
            Q(title__icontains=generic_term) |
            Q(court_name__icontains=generic_term) |
            Q(petitioner__icontains=generic_term) |
            Q(respondent__icontains=generic_term) |
            Q(project__icontains=generic_term) |
            Q(assigned_to__first_name__icontains=generic_term) |
            Q(assigned_to__last_name__icontains=generic_term) |
            Q(assigned_to__username__icontains=generic_term)
        ).exists()
        if has_case_hits:
            return JsonResponse({
                'ok': True,
                'type': 'navigate',
                'url': f'/cases/?search={generic_term}',
                'message': f'Found matching CRM records for "{generic_term}". Opening case search.'
            })

    help_text = (
        "I can answer from your software data using keyword matching.\n"
        "Examples:\n"
        "- 'total cases', 'pending cases', 'overdue deadlines', 'pending tasks'\n"
        "- 'case title of ABC/123/2024/1'\n"
        "- 'next hearing date for xyz case'\n"
        "- 'status and court for [party/case name]'\n"
        "- 'show cases for [project/party/court]'\n"
        "I can also navigate to pages like dashboard, case list, workload, analytics, and notifications.\n"
        "Tip: type `/ai your question` or `ai: your question` for Gemini (needs GEMINI_API_KEY on the server)."
    )
    if gemini_service.is_configured():
        try:
            ai_text = _legi_gemini_reply(request.user, raw_message)
            footer = (
                "\n\n—\nShortcuts: try phrases like 'dashboard', 'case list', or 'pending cases' "
                "for instant navigation without AI."
            )
            return JsonResponse({'ok': True, 'type': 'text', 'message': ai_text + footer})
        except Exception:
            pass
    return JsonResponse({'ok': True, 'type': 'text', 'message': help_text})
