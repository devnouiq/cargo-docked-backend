"""fix legacy lowercase tracking_status values that crash on read

Root cause (found live, 2026-09-16): `TrackedContainer.tracking_status` is
declared as `sa.Enum(ContainerScrapeStatus, native_enum=False, length=20)`
with no `values_callable` - SQLAlchemy's default convention for a Python
Enum column is to store/expect the member's *name* (`"COMPLETED"`), not its
`.value` (`"completed"`), confirmed by direct reproduction: an ORM-written
row stores `'COMPLETED'`, but a row containing the lowercase string
`'completed'` raises `LookupError: 'completed' is not among the defined
enum values` on every read.

`4c1f8a9d2e7b_add_container_scrape_status.py` added this column with
`server_default="succeeded"` (lowercase) - Postgres backfills that literal
string onto every row that already existed at that ALTER TABLE, bypassing
the ORM's name-based convention entirely. `dc0970f7add9_rename_container_
tracking_fields.py` then ran `UPDATE ... SET tracking_status = 'completed'
WHERE tracking_status = 'succeeded'` - again a raw lowercase literal. Any
row that predates the original column addition has been silently broken
ever since: every `GET`/list touching one of these rows raises the
LookupError above (observed live as 500s deep enough to occasionally
surface as a Cloud Run 503) - this is very plausibly the real cause behind
a client report of "some containers return tracking_status: null with no
explanation" and "some containers just never resolve": a crash on read
looks exactly like that from a client's point of view (no valid response
comes back to explain why).

This migration only fixes the stored *representation* (lowercase -> the
uppercase name the column already expects) - it does not touch the model's
`Enum(...)` declaration itself. That's intentional: normalizing existing
data to match the ORM's current, already-deployed expectation is a strictly
smaller, safer change than also flipping the column to a `values_callable`
(lowercase) convention in the same release - the latter is worth doing
separately, deliberately, not as a side effect of an urgent data fix.

Idempotent: only rows still holding one of the 5 legacy lowercase values
are touched; already-correct (uppercase) rows are untouched, so this is
safe to run more than once.

Revision ID: 9a3d5f1c8b42
Revises: 7f2b4c9a1d63
Create Date: 2026-09-16 19:45:00.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "9a3d5f1c8b42"
down_revision: Union[str, Sequence[str], None] = "7f2b4c9a1d63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LOWERCASE_TO_NAME = {
    "queued": "QUEUED",
    "in_progress": "IN_PROGRESS",
    "completed": "COMPLETED",
    "no_data": "NO_DATA",
    "failed": "FAILED",
}


def upgrade() -> None:
    for lowercase, name in _LOWERCASE_TO_NAME.items():
        op.execute(
            f"UPDATE tracked_containers SET tracking_status = '{name}' "
            f"WHERE tracking_status = '{lowercase}'"
        )


def downgrade() -> None:
    # Deliberately a no-op: the pre-fix state was a data bug (rows that
    # crashed on read), not a valid prior state worth restoring.
    pass
