#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════╗
# ║  LegalCRM — One-click setup                              ║
# ║  Usage: bash setup.sh                                    ║
# ╚══════════════════════════════════════════════════════════╝
set -e
cd "$(dirname "$0")"

echo ""
echo "⚖  LegalCRM Setup"
echo "────────────────────────────────────────"

# Virtual environment
if [ ! -d "venv" ]; then
    echo "▶  Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

# Install Django
echo "▶  Installing Django..."
pip install "Django>=4.2,<5.0" --quiet

# Run all migrations (0001 → 0005 for cases, 0001 → 0002 for accounts)
echo "▶  Running migrations..."
python manage.py migrate

# Seed demo data
echo "▶  Seeding demo users and cases..."
python manage.py seed_data

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ✅  Setup complete!                                      ║"
echo "║                                                           ║"
echo "║  Start:   python manage.py runserver                      ║"
echo "║  Open:    http://127.0.0.1:8000/                          ║"
echo "║                                                           ║"
echo "║  Login credentials:                                       ║"
echo "║    Admin      admin       / admin123                      ║"
echo "║    Lawyer 1   adv_sharma  / lawyer123                     ║"
echo "║    Lawyer 2   adv_gupta   / lawyer123                     ║"
echo "║    Assistant  assistant1  / assist123                     ║"
echo "║    Client     client1     / client123                     ║"
echo "║                                                           ║"
echo "║  Schedule daily reminders (add to crontab):               ║"
echo "║    0 8 * * * python manage.py send_reminders              ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

read -p "Start the server now? [Y/n]: " start
if [[ "$start" != "n" && "$start" != "N" ]]; then
    python manage.py runserver
fi
