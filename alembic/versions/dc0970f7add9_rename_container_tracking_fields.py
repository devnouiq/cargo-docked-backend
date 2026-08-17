"""rename scrape_status/scrape_error to tracking_status/tracking_message,
drop provider_name from tracked_containers

Client-requested change: `scrape_status`/`scrape_error`/`provider_name` are
internal scraping-implementation details that were leaking onto the
customer-facing `ContainerOut` schema (`app/schemas/container.py`) and the
`container.updated` webhook payload. Renamed to product-facing names
(`tracking_status`/`tracking_message`) and dropped `provider_name` from the
table entirely - see `app/models/container.py`/`app/schemas/container.py`
for the corresponding model/schema changes.

Also backfills any existing `'succeeded'` values in the (renamed)
`tracking_status` column to `'completed'`, matching the
`ContainerScrapeStatus.SUCCEEDED` -> `ContainerScrapeStatus.COMPLETED` enum
member rename in the same change (value `"succeeded"` -> `"completed"`;
the other members - `queued`/`in_progress`/`no_data`/`failed` - are
unchanged).

Hand-written for the same reason as every other migration in this repo
(`alembic revision --autogenerate` needs a live DB to diff against).

`op.alter_column(..., new_column_name=...)` and `op.drop_column(...)` below
are correct, straightforward Alembic/Postgres syntax and are the primary
target (`DATABASE_URL` is Postgres in every real environment - see
CLAUDE.md). SQLite (used for the local hermetic test suite / the
`DATABASE_URL=sqlite:///./scratch.db` smoke check) has much more limited
`ALTER TABLE` support than Postgres - a plain `op.alter_column` rename can
fail there depending on the installed SQLAlchemy/Alembic/pysqlite version,
even though the exact same call is correct against Postgres. If the SQLite
smoke check fails specifically on the rename step, that's a SQLite-dialect
limitation, not a bug in this migration - verify against real Postgres
before relying on that result.

Revision ID: dc0970f7add9
Revises: 87da9dc56852
Create Date: 2026-08-17 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "dc0970f7add9"
down_revision: Union[str, Sequence[str], None] = "87da9dc56852"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "tracked_containers",
        "scrape_status",
        new_column_name="tracking_status",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        existing_server_default="succeeded",
    )
    op.alter_column(
        "tracked_containers",
        "scrape_error",
        new_column_name="tracking_message",
        existing_type=sa.String(length=500),
        existing_nullable=True,
    )
    op.drop_column("tracked_containers", "provider_name")

    op.execute("UPDATE tracked_containers SET tracking_status = 'completed' WHERE tracking_status = 'succeeded'")

    # The column's DB-level server_default was 'succeeded' (set by the
    # historical migration that first added it) - no longer a valid
    # ContainerScrapeStatus member after this rename. The app never relies
    # on it (the model sets a Python-side default of QUEUED on every
    # insert), but a stale/invalid server_default is a footgun for any
    # future raw SQL insert, so bring it in line with the app's default.
    op.alter_column(
        "tracked_containers",
        "tracking_status",
        server_default="queued",
    )


def downgrade() -> None:
    op.alter_column(
        "tracked_containers",
        "tracking_status",
        server_default="succeeded",
    )

    op.execute("UPDATE tracked_containers SET tracking_status = 'succeeded' WHERE tracking_status = 'completed'")

    op.add_column("tracked_containers", sa.Column("provider_name", sa.String(length=50), nullable=True))
    op.alter_column(
        "tracked_containers",
        "tracking_message",
        new_column_name="scrape_error",
        existing_type=sa.String(length=500),
        existing_nullable=True,
    )
    op.alter_column(
        "tracked_containers",
        "tracking_status",
        new_column_name="scrape_status",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        existing_server_default="succeeded",
    )
