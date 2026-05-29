from django.contrib.auth.models import Group
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

# Django-Gruppe mit Mitarbeiterinnen-Standardperms (view/add/change, ohne delete).
# Wird per Datamigration 0007 angelegt.
MITARBEITERIN_GROUP = 'Stockkeeper-Mitarbeiterin'


class StockKeeperOIDCBackend(OIDCAuthenticationBackend):
    """OIDC Backend für Stock Keeper.

    Matcht Authentik-User per Email auf Django-User. Setzt is_staff anhand der
    Authentik-Gruppe 'mitarbeiterin' (Standard-Praxisteam) bzw. 'geschaeftsfuehrung'.
    Geschäftsführung wird zusätzlich als Superuser markiert.
    Mitarbeiterinnen werden in die Django-Gruppe MITARBEITERIN_GROUP gepackt,
    damit sie die nötigen Permissions (Sidebar, Produktsuche etc.) haben.
    """

    def filter_users_by_claims(self, claims):
        email = claims.get('email')
        if not email:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(email__iexact=email)

    def create_user(self, claims):
        user = super().create_user(claims)
        self._sync_user(user, claims)
        return user

    def update_user(self, user, claims):
        self._sync_user(user, claims)
        return user

    def _sync_user(self, user, claims):
        groups = claims.get('groups', [])
        is_gf = 'geschaeftsfuehrung' in groups
        # 'stockkeeper_only' = dedizierte Authentik-Gruppe für User, die ausschliesslich
        # Stock-Keeper-Zugang haben sollen (keine Workbench/Mamifit/Website).
        is_team = is_gf or 'mitarbeiterin' in groups or 'stockkeeper_only' in groups

        user.first_name = claims.get('given_name', '')
        user.last_name = claims.get('family_name', '')
        user.email = claims.get('email', '')
        user.is_staff = is_team
        user.is_superuser = is_gf
        user.save(update_fields=['first_name', 'last_name', 'email', 'is_staff', 'is_superuser'])

        # Gruppenzuweisung: Mitarbeiterinnen ohne Superuser-Status brauchen die
        # Default-Perms, sonst ist die Jazzmin-Sidebar leer und die Produktsuche
        # liefert 403. Geschäftsführung hat als Superuser eh alles.
        try:
            mitarbeiter_group = Group.objects.get(name=MITARBEITERIN_GROUP)
        except Group.DoesNotExist:
            return
        if is_team and not is_gf:
            user.groups.add(mitarbeiter_group)
        else:
            user.groups.remove(mitarbeiter_group)
