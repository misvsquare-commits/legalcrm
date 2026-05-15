"""
Management command: seed_data
Creates demo users, cases, outcomes, and time entries.
Usage:  python manage.py seed_data
"""
import random
import decimal
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Seed the database with demo users and sample cases'

    def handle(self, *args, **options):
        from accounts.models import User
        from cases.models import (Case, CaseUpdate, Comment,
                                   CaseOutcome, TimeEntry,
                                   CaseDeadline, CaseTask)

        self.stdout.write(self.style.MIGRATE_HEADING('\n🌱  Seeding demo data...\n'))
        today = timezone.now().date()

        # ── Users ────────────────────────────────────────────────────────────
        users_spec = [
            dict(username='admin',      password='admin123',  role='admin',
                 first_name='Admin',    last_name='User',
                 email='admin@legalcrm.com',
                 is_superuser=True, is_staff=True, bar_number=''),
            dict(username='adv_sharma', password='lawyer123', role='lawyer',
                 first_name='Rajesh',   last_name='Sharma',
                 email='sharma@legalcrm.com',
                 is_superuser=False, is_staff=False, bar_number='DL/2010/1234'),
            dict(username='adv_gupta',  password='lawyer123', role='lawyer',
                 first_name='Priya',    last_name='Gupta',
                 email='gupta@legalcrm.com',
                 is_superuser=False, is_staff=False, bar_number='DL/2015/5678'),
            dict(username='assistant1', password='assist123', role='assistant',
                 first_name='Anita',    last_name='Singh',
                 email='anita@legalcrm.com',
                 is_superuser=False, is_staff=False, bar_number=''),
            dict(username='client1',    password='client123', role='client',
                 first_name='Ramesh',   last_name='Verma',
                 email='client@example.com',
                 is_superuser=False, is_staff=False, bar_number=''),
        ]

        created_users = {}
        for spec in users_spec:
            password    = spec.pop('password')
            is_super    = spec.pop('is_superuser')
            is_staff    = spec.pop('is_staff')
            bar_number  = spec.pop('bar_number')
            username    = spec['username']

            user = User.objects.filter(username=username).first()
            if user is None:
                user = User(**spec)
                user.set_password(password)
                user.is_superuser = is_super
                user.is_staff     = is_staff
                user.bar_number   = bar_number
                user.save()
                self.stdout.write(self.style.SUCCESS(f'  ✓ Created user : {username}'))
            else:
                self.stdout.write(f'  → Already exists: {username}')
            created_users[username] = user

        sharma = created_users['adv_sharma']
        gupta  = created_users['adv_gupta']
        admin  = created_users['admin']

        # ── Cases ─────────────────────────────────────────────────────────────
        cases_data = [
            dict(case_number='HC/DEL/2024/001',
                 title='Ram Kumar vs State of Delhi',
                 project='Criminal Appeals 2024',
                 court_name='High Court of Delhi', case_type='criminal',
                 filing_date=today - timezone.timedelta(days=120),
                 last_hearing_date=today - timezone.timedelta(days=10),
                 next_hearing_date=today + timezone.timedelta(days=3),
                 status='in_progress', petitioner='Ram Kumar', respondent='State of Delhi',
                 description='Criminal appeal against conviction under IPC Section 302.',
                 assigned_to=sharma),
            dict(case_number='SC/2024/1234',
                 title='Tech Corp India Ltd vs Innovations Pvt Ltd',
                 project='IP & Corporate Disputes',
                 court_name='Supreme Court of India', case_type='corporate',
                 filing_date=today - timezone.timedelta(days=90),
                 last_hearing_date=today - timezone.timedelta(days=5),
                 next_hearing_date=today + timezone.timedelta(days=1),
                 status='in_progress', petitioner='Tech Corp India Ltd',
                 respondent='Innovations Pvt Ltd',
                 description='Dispute regarding intellectual property and breach of contract.',
                 assigned_to=gupta),
            dict(case_number='DC/2023/5678',
                 title='Suresh Mehta vs Anita Mehta',
                 project='Family Law Matters',
                 court_name='District Court Saket', case_type='family',
                 filing_date=today - timezone.timedelta(days=200),
                 last_hearing_date=today - timezone.timedelta(days=20),
                 next_hearing_date=today + timezone.timedelta(days=14),
                 status='in_progress', petitioner='Suresh Mehta', respondent='Anita Mehta',
                 description='Divorce petition under Section 13 of Hindu Marriage Act.',
                 assigned_to=sharma),
            dict(case_number='HC/BOM/2023/789',
                 title='Property Dispute - Kohinoor Estate',
                 project='Property Matters',
                 court_name='Bombay High Court', case_type='property',
                 filing_date=today - timezone.timedelta(days=300),
                 last_hearing_date=today - timezone.timedelta(days=30),
                 next_hearing_date=None, status='disposed',
                 petitioner='Vikram Malhotra', respondent='Kohinoor Developers',
                 description='Property ownership dispute. Case disposed in favour of petitioner.',
                 assigned_to=gupta),
            dict(case_number='NCLT/2024/456',
                 title='Insolvency Proceedings - Alpha Manufacturing',
                 project='IP & Corporate Disputes',
                 court_name='National Company Law Tribunal', case_type='corporate',
                 filing_date=today - timezone.timedelta(days=60),
                 last_hearing_date=today - timezone.timedelta(days=2),
                 next_hearing_date=today,
                 status='in_progress', petitioner='SBI Bank',
                 respondent='Alpha Manufacturing Ltd',
                 description='Insolvency resolution proceedings under IBC 2016.',
                 assigned_to=sharma),
            dict(case_number='HC/DEL/2024/222',
                 title='Labour Dispute - Rajdhani Textiles',
                 project='Labour & Employment',
                 court_name='High Court of Delhi', case_type='labour',
                 filing_date=today - timezone.timedelta(days=45),
                 last_hearing_date=today - timezone.timedelta(days=15),
                 next_hearing_date=today + timezone.timedelta(days=21),
                 status='on_hold', petitioner='Workers Union',
                 respondent='Rajdhani Textiles Ltd',
                 description='Unlawful termination of 50 workers. Case on hold pending mediation.',
                 assigned_to=gupta),
        ]

        for cd in cases_data:
            assigned = cd.pop('assigned_to')
            if Case.objects.filter(case_number=cd['case_number']).exists():
                self.stdout.write(f'  → Case exists  : {cd["case_number"]}')
                continue
            case = Case.objects.create(**cd, assigned_to=assigned, created_by=admin)
            self.stdout.write(self.style.SUCCESS(f'  ✓ Created case  : {case.case_number}'))

            CaseUpdate.objects.create(
                case=case, title='Initial Hearing Held',
                date=today - timezone.timedelta(days=random.randint(5, 30)),
                description='Matter was heard by the Honourable Court. Arguments were presented by both parties. Next date fixed.',
                next_action_date=case.next_hearing_date, created_by=assigned,
            )
            Comment.objects.create(
                case=case, user=assigned,
                comment='Case documents filed and acknowledged by the registry.',
            )

            # Deadline
            if case.status != 'disposed':
                CaseDeadline.objects.create(
                    case=case, title='File written submissions',
                    due_date=today + timezone.timedelta(days=random.randint(7, 30)),
                    priority='high', created_by=assigned,
                )

            # Task
            if case.status != 'disposed':
                CaseTask.objects.create(
                    case=case, title='Collect supporting affidavit from client',
                    assigned_to=assigned,
                    due_date=today + timezone.timedelta(days=random.randint(3, 14)),
                    created_by=admin,
                )

        # ── Outcomes ─────────────────────────────────────────────────────────
        disposed_cases = Case.objects.filter(status='disposed')
        for case in disposed_cases:
            if not CaseOutcome.objects.filter(case=case).exists():
                CaseOutcome.objects.create(
                    case=case, result='won',
                    summary='Property ownership confirmed in favour of petitioner. All documentary evidence accepted.',
                    disposed_date=today - timezone.timedelta(days=random.randint(1, 60)),
                    recorded_by=admin,
                )
                self.stdout.write(self.style.SUCCESS(f'  ✓ Outcome       : {case.case_number}'))

        # ── Time entries ──────────────────────────────────────────────────────
        active_cases = list(Case.objects.exclude(status='disposed')[:4])
        entries = ['Attended hearing', 'Drafted reply brief', 'Client consultation',
                   'Research on precedents', 'Filed vakalatnama', 'Reviewed documents']
        for i, case in enumerate(active_cases):
            lawyer = [sharma, gupta][i % 2]
            for j in range(2):
                if not TimeEntry.objects.filter(case=case, user=lawyer).exists():
                    TimeEntry.objects.create(
                        case=case, user=lawyer,
                        date=today - timezone.timedelta(days=random.randint(1, 30)),
                        hours=decimal.Decimal(str(round(random.uniform(0.5, 3.0) * 4) / 4)),
                        description=entries[(i * 2 + j) % len(entries)],
                        is_billable=True,
                    )

        self.stdout.write('\n' + '─' * 50)
        self.stdout.write(self.style.SUCCESS('✅  Demo data seeded!'))
        self.stdout.write('─' * 50)
        self.stdout.write('  Admin      admin       /  admin123')
        self.stdout.write('  Lawyer 1   adv_sharma  /  lawyer123')
        self.stdout.write('  Lawyer 2   adv_gupta   /  lawyer123')
        self.stdout.write('  Assistant  assistant1  /  assist123')
        self.stdout.write('  Client     client1     /  client123')
        self.stdout.write('─' * 50)
        self.stdout.write('  Run: python manage.py runserver')
        self.stdout.write('  Open: http://127.0.0.1:8000/\n')
