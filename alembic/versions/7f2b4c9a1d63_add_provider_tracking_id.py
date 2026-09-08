"""add provider_tracking_id to tracked_containers

GoComet's create-then-poll tracking flow (app/providers/gocomet_http.py,
part of the SeaRates -> GoComet provider swap) issues its own tracking-
request id per container. Persisting it lets a later refresh resume polling
an already-created GoComet tracking record instead of paying for a fresh
create call against GoComet's *monthly* quota if a previous attempt crashed
mid-poll - see app/services/container_service.py's `_persist_tracking_id_early`
(committed immediately after create, before polling starts) and
`_refresh_and_apply` (passes it back in as `resume_id` on the next attempt).

Nullable, no default: only ever set by a GoComet-backed lookup; every other
provider (Romeu, and SeaRates historically) leaves it null.

Hand-written for the same reason as every other migration in this repo
(`alembic revision --autogenerate` needs a live DB to diff against).

Revision ID: 7f2b4c9a1d63
Revises: dc0970f7add9
Create Date: 2026-09-08 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "7f2b4c9a1d63"
down_revision: Union[str, Sequence[str], None] = "dc0970f7add9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tracked_containers",
        sa.Column("provider_tracking_id", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tracked_containers", "provider_tracking_id")
