"""Login-required + role-based access middleware."""
from django.shortcuts import redirect
from django.urls import reverse


PUBLIC_PATH_PREFIXES = (
    '/login/',
    '/logout/',
    '/admin/login/',
    '/static/',
    '/favicon',
    '/t/o/',         # email open tracking pixel
    '/t/c/',         # email click tracking redirect
    '/u/',           # unsubscribe — the recipient is never logged in
    '/meet/',        # public booking pages
    '/api/sync/',    # local→prod sync (token-authed)
)

# Callers (non-admin/non-manager) can ONLY access these prefixes
CALLER_ALLOWED_PREFIXES = (
    '/calling/',
    '/route/',
    '/logout/',
    '/static/',
    '/media/',
    '/favicon',
    '/notifications/',
    '/mentions/',
    '/calendar/',
    '/t/',
    '/meet/',
)

# Caller-only paths — admins should NOT land here (auto-redirect to admin dashboard)
CALLER_ONLY_PREFIXES = (
    '/calling/',
)


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Public path? skip auth check
        if any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
            return self.get_response(request)

        # Not authenticated? Send to login
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={path}")

        user = request.user
        profile = getattr(user, 'caller_profile', None)
        role = profile.role if profile else ('admin' if user.is_superuser else 'caller')
        is_admin = user.is_superuser or role == 'admin'
        is_manager = role == 'manager'
        is_caller = (role == 'caller' and not user.is_superuser)

        # === Admin / Manager hitting caller-only routes ===
        # Don't redirect — let them view (admins can supervise) — but no auto-bounce
        # EXCEPTION: hitting bare /calling/ — show admin dashboard instead so they
        # don't see the caller workspace by mistake when navigating.
        # Manager can still view caller workspace.
        if is_admin and path == '/calling/':
            return redirect('dashboard')

        # === Caller hitting admin routes ===
        if is_caller:
            if not any(path.startswith(p) for p in CALLER_ALLOWED_PREFIXES):
                return redirect('caller_dashboard')

        return self.get_response(request)
