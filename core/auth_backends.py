from django.contrib.auth.backends import ModelBackend


class SuperuserOnlyModelBackend(ModelBackend):
    """Passwort-Login nur noch als Notfallzugang für Superuser.

    Alle anderen melden sich ausschliesslich via Authentik (OIDC) an. Die
    Permission-Prüfung (has_perm, Gruppen) bleibt für alle User aktiv, da das
    OIDC-Backend selbst keine Permissions auflöst.
    """

    def user_can_authenticate(self, user):
        return super().user_can_authenticate(user) and user.is_superuser
