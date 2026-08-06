"""Seed local/dev data: default billing plans + one dev org/user/API key.

Schema is Alembic's job now, not this script's - run `alembic upgrade
head` first (see CLAUDE.md). Superseded here: the old version of this
script called `models.Base.metadata.create_all()` and created a
`Customer` row directly; both the flat table-creation and the `Customer`
model are gone (see app/models/api_key.py, app/models/organization.py).
"""

from __future__ import annotations

from app.db.session import SessionLocal
from app.models.api_key import ApiKeyMode
from app.services.api_key_service import ApiKeyService
from app.services.auth_service import AuthService
from app.services.billing_service import seed_default_plans

DEV_EMAIL = "dev@example.com"
DEV_PASSWORD = "dev-password-123"
DEV_ORG_NAME = "Dev Org"


def main() -> None:
    with SessionLocal() as db:
        seed_default_plans(db)
        print("Seeded default plans (free/starter/growth/enterprise).")

        auth = AuthService()
        user = auth.users.get_by_email(db, DEV_EMAIL)
        if user is None:
            user, org = auth.signup(
                db, email=DEV_EMAIL, password=DEV_PASSWORD, full_name="Dev User", organization_name=DEV_ORG_NAME
            )
            print(f"Created dev user {DEV_EMAIL} / {DEV_PASSWORD} in org {org.slug!r}.")
        else:
            org = auth._primary_organization(db, user)
            print(f"Dev user {DEV_EMAIL} already exists (org {org.slug!r}).")

        key_service = ApiKeyService()
        existing_keys = key_service.list_for_org(db, org.id)
        if existing_keys:
            print(f"Org already has {len(existing_keys)} API key(s); not creating another.")
            return

        _key, raw_key = key_service.create(
            db, organization_id=org.id, created_by_user_id=user.id, name="Dev sandbox key",
            mode=ApiKeyMode.SANDBOX, scopes=[],
        )
        print(f"Created API key (shown once): {raw_key}")


if __name__ == "__main__":
    main()
