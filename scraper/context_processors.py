"""Template context shared across every page."""
from django.conf import settings


def feature_flags(request):
    """Expose deployment capabilities to templates.

    Scraping needs a local Chrome binary, so it only runs where one exists —
    the operator's own machine, not the VPS.
    """
    return {
        'SCRAPING_ENABLED': getattr(settings, 'SCRAPING_ENABLED', True),
    }
