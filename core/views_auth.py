from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import redirect


def sso_logout(request):
    """Logout aus Django + Redirect zu Authentik End-Session."""
    logout(request)
    authentik_logout = getattr(settings, 'OIDC_OP_LOGOUT_ENDPOINT', '')
    if authentik_logout:
        return redirect(authentik_logout)
    return redirect(settings.LOGOUT_REDIRECT_URL)
