"""add usd pricing to plans

Hand-written for the same reason as the other migrations in this repo
(`alembic revision --autogenerate` needs a live DB to diff against).

Adds a USD counterpart to the existing EUR-only `monthly_price_cents` /
`stripe_price_id` columns on `plans` - both nullable, matching
`stripe_price_id`'s existing "optional, populate once a real Stripe object
exists in this environment" contract. Free/Enterprise plans never get a
value in either new column (no Stripe object at all); Starter/Growth get
one once a USD Stripe Price is created and STRIPE_STARTER_PRICE_ID_USD /
STRIPE_GROWTH_PRICE_ID_USD are set (see .env.example, app/core/config.py).

Revision ID: 87da9dc56852
Revises: 621e88fbb0c4
Create Date: 2026-08-17 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "87da9dc56852"
down_revision: Union[str, Sequence[str], None] = "621e88fbb0c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("monthly_price_cents_usd", sa.Integer(), nullable=True))
    op.add_column("plans", sa.Column("stripe_price_id_usd", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("plans", "stripe_price_id_usd")
    op.drop_column("plans", "monthly_price_cents_usd")
