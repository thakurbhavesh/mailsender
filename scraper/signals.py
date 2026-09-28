"""Auth signals: open/close attendance on login/logout."""
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from .calling_utils import open_attendance, close_attendance, get_profile, is_caller


@receiver(user_logged_in)
def on_user_login(sender, request, user, **kwargs):
    # Ensure profile exists
    get_profile(user)
    # Only callers get attendance rows
    if is_caller(user):
        open_attendance(user, request=request)


@receiver(user_logged_out)
def on_user_logout(sender, request, user, **kwargs):
    if user and is_caller(user):
        close_attendance(user)
