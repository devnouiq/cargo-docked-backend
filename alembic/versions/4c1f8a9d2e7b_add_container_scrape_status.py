"""add scrape_status/scrape_error to tracked_containers

Hand-written for the same reason as the initial migration
(`alembic revision --autogenerate` needs a live DB to diff against).
`sa.String(length=20)` rather than `sa.Enum` because the model declares
`Enum(..., native_enum=False)` - stored as VARCHAR, no CREATE TYPE dance.

`scrape_status` gets `server_default="succeeded"` purely as a rolling-deploy
safety net: during the window where old API code is still inserting rows
without knowing about this column, a DB-level default keeps those INSERTs
from failing the NOT NULL. New code always supplies the value itself
(the model's Python-side `default=QUEUED` wins), so this server default
never actually decides what a new row looks like.

Backfill: rows that never resolved (`status IS NULL`) are `no_data`, not
`succeeded` - they got a clean provider run that found nothing, which is
exactly what `no_data` means. Everything else keeps the `succeeded`
server default.

Revision ID: 4c1f8a9d2e7b
Revises: 192b867b97bf
Create Date: 2026-08-11 09:41:18.220314

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "4c1f8a9d2e7b"
down_revision: Union[str, Sequence[str], None] = "192b867b97bf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tracked_containers",
        sa.Column("scrape_status", sa.String(length=20), nullable=False, server_default="succeeded"),
    )
    op.add_column("tracked_containers", sa.Column("scrape_error", sa.String(length=500), nullable=True))

    op.execute("UPDATE tracked_containers SET scrape_status = 'no_data' WHERE status IS NULL")


def downgrade() -> None:
    op.drop_column("tracked_containers", "scrape_error")
    op.drop_column("tracked_containers", "scrape_status")
