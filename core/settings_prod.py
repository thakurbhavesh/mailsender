"""Production settings — loaded when DJANGO_SETTINGS_MODULE=core.settings_prod.

Reads secrets from environment variables. Set these on your VPS:
    SECRET_KEY=<random 50+ char string>
    ALLOWED_HOSTS=leadhunt.com,www.leadhunt.com
    DB_HOST=localhost  DB_PORT=5432  DB_NAME=leadhunt  DB_USER=leadhunt  DB_PASSWORD=<strong>
    SYNC_API_TOKEN=<random 40+ char string — same as PROD_SYNC_TOKEN on your local machine>

Optional:
    CSRF_TRUSTED_ORIGINS=https://leadhunt.com,https://www.leadhunt.com
    SENTRY_DSN=https://...@sentry.io/...
    TRACKING_BASE_URL=https://leadhunt.com
"""
import os
from .settings import *  # noqa — inherit everything, then override

# ---------- SECURITY ----------
DEBUG = False
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError('SECRET_KEY must be set in environment for production.')

ALLOWED_HOSTS = [h.strip() for h in os.environ.get('ALLOWED_HOSTS', '').split(',') if h.strip()]
if not ALLOWED_HOSTS:
    raise RuntimeError('ALLOWED_HOSTS must be set (e.g. ALLOWED_HOSTS=leadhunt.com,www.leadhunt.com)')

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get(
        'CSRF_TRUSTED_ORIGINS',
        ','.join(f'https://{h}' for h in ALLOWED_HOSTS)
    ).split(',') if o.strip()
]

# HTTPS hardening
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'

# ---------- DATABASE ----------
# Render gives DATABASE_URL like postgres://user:pass@host:5432/dbname
# Self-hosted VPS uses individual DB_* env vars
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if DATABASE_URL:
    # Parse DATABASE_URL (Render, Heroku, etc.)
    from urllib.parse import urlparse
    url = urlparse(DATABASE_URL)
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': url.path[1:],
            'USER': url.username,
            'PASSWORD': url.password,
            'HOST': url.hostname,
            'PORT': url.port or 5432,
            'CONN_MAX_AGE': 60,
            'OPTIONS': {'connect_timeout': 10, 'sslmode': 'require'},
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('DB_NAME', 'leadhunt'),
            'USER': os.environ.get('DB_USER', 'leadhunt'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', 'localhost'),
            'PORT': os.environ.get('DB_PORT', '5432'),
            'CONN_MAX_AGE': 60,
            'OPTIONS': {'connect_timeout': 10},
        }
    }

# ---------- STATIC + MEDIA ----------
STATIC_ROOT = BASE_DIR / 'staticfiles'  # noqa — collectstatic dumps here
STATIC_URL = '/static/'

# WhiteNoise for static file serving (no separate Nginx config needed)
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # right after SecurityMiddleware
] + [m for m in MIDDLEWARE if m != 'django.middleware.security.SecurityMiddleware']  # noqa

STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Media — keep on local disk for now (move to S3 later if needed)
MEDIA_ROOT = BASE_DIR / 'media'
MEDIA_URL = '/media/'

# ---------- LOGGING ----------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'django.log',
            'maxBytes': 10 * 1024 * 1024,  # 10 MB
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'root': {'handlers': ['console', 'file'], 'level': 'INFO'},
    'loggers': {
        'django.request': {'handlers': ['file'], 'level': 'ERROR', 'propagate': False},
    },
}
os.makedirs(BASE_DIR / 'logs', exist_ok=True)

# ---------- SENTRY (optional but recommended) ----------
SENTRY_DSN = os.environ.get('SENTRY_DSN', '').strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration()],
            traces_sample_rate=0.1,
            send_default_pii=False,
            environment='production',
        )
    except ImportError:
        pass  # sentry-sdk not installed

# ---------- TIME ----------
# Keep IST from base settings
TIME_ZONE = 'Asia/Kolkata'
USE_TZ = True

# ---------- EMAIL OUTBOUND TRACKING URL ----------
# Used in email_service.py for open/click pixel base URL
# Default to first ALLOWED_HOST if not set
if not os.environ.get('TRACKING_BASE_URL'):
    os.environ['TRACKING_BASE_URL'] = f'https://{ALLOWED_HOSTS[0]}'
