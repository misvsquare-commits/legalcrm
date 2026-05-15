"""
Django settings for Legal Management CRM
Production-ready configuration with MySQL support
"""

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = 'django-insecure-change-this-in-production-use-env-variable'

DEBUG = True  # Set to False in production

ALLOWED_HOSTS = ['*']  # Restrict in production
CSRF_TRUSTED_ORIGINS = [
    "https://legalcrm-production.up.railway.app",
]
# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    # Local apps
    'accounts',
    'cases',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'legal_crm.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'legal_crm.wsgi.application'

# Database - MySQL Configuration
# For SQLite (development/testing without MySQL):
# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.sqlite3',
#         'NAME': BASE_DIR / 'db.sqlite3',
#     }
# }
# Testing for Railway Start
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get('MYSQLDATABASE'),
        'USER': os.environ.get('MYSQLUSER'),
        'PASSWORD': os.environ.get('MYSQLPASSWORD'),
        'HOST': os.environ.get('MYSQLHOST'),
        'PORT': os.environ.get('MYSQLPORT'),
    }
}
# End
# MySQL Configuration (uncomment and configure for production):
# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.mysql',
#         'NAME': 'legal_crm_db',
#         'USER': 'root',
#         'PASSWORD': '',
#         'HOST': 'localhost',
#         'PORT': '3306',
#         'OPTIONS': {
#             'charset': 'utf8mb4',
#             'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
#         },
#     }
# }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (uploaded documents)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Auth URLs
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/dashboard/'  # overridden per-role in accounts/views.py
LOGOUT_REDIRECT_URL = '/login/'

# Custom User Model
AUTH_USER_MODEL = 'accounts.User'

# File upload settings
MAX_UPLOAD_SIZE = 10485760  # 10MB
ALLOWED_DOCUMENT_TYPES = [
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'image/jpeg',
    'image/png',
]

# ── Email configuration ────────────────────────────────────────────────────
# Defaults to console in DEBUG unless EMAIL_BACKEND is explicitly set.
SITE_NAME = os.environ.get('SITE_NAME', 'LegalCRM')
SITE_URL  = os.environ.get('SITE_URL', 'http://127.0.0.1:8000')
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND',
    'django.core.mail.backends.smtp.EmailBackend',
)
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.dreamhost.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', True)
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', 'mis.automation@vsquareservices.com')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', 'Legend@7570#Square')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER)

# ── Notification settings ─────────────────────────────────────────────────
HEARING_REMINDER_DAYS = [7, 1]  # Send reminders 7 days and 1 day before hearing

# ── Google Gemini (optional — for Legi, summaries, document drafts) ──────
# Free tier: create an API key in Google AI Studio and export it as env var.
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
# Examples: gemini-2.0-flash, gemini-1.5-flash, gemini-1.5-pro
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
