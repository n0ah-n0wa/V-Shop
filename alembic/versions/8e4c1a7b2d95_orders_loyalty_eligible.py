"""orders.loyalty_eligible: stamps only for orders placed after launch

NON-DESTRUCTIVE. One column on ``orders``::

    loyalty_eligible BOOLEAN NOT NULL DEFAULT false

PostgreSQL 11+ adds a column with a constant default without rewriting the
table, so this is instant however long the order history is. No existing value
is modified: every order present when it runs — including orders still in
progress — reads ``false`` and never earns stamps (owner decision: the loyalty
programme applies only to new orders). The application marks every order it
places from now on ``true``.

Downgrade drops the column. Re-upgrading would then mark *every* order
ineligible, including those placed after launch and not yet completed, so the
downgrade refuses while eligible orders exist unless told explicitly::

    alembic -x allow_loyalty_data_loss=true downgrade 3b9d6f2a8c14

Revision ID: 8e4c1a7b2d95
Revises: 3b9d6f2a8c14
Create Date: 2026-09-10 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "8e4c1a7b2d95"
down_revision: str | None = "3b9d6f2a8c14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_to_forget_eligibility() -> None:
    """Stop a downgrade that would strip stamps from orders already promised them."""
    if context.is_offline_mode():
        return  # emitting SQL for review: there is no database to inspect
    flag = context.get_x_argument(as_dictionary=True).get("allow_loyalty_data_loss", "")
    allowed = str(flag).strip().lower() in {"1", "true", "yes"}
    eligible = int(
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM orders WHERE loyalty_eligible"))
        .scalar()
        or 0
    )
    if eligible and not allowed:
        raise RuntimeError(
            f"Refusing to downgrade 8e4c1a7b2d95: {eligible} order(s) placed under the "
            "loyalty programme would lose their eligibility for stamps. Re-run with "
            "`alembic -x allow_loyalty_data_loss=true downgrade <revision>` if intended."
        )


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("loyalty_eligible", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    _refuse_to_forget_eligibility()
    op.drop_column("orders", "loyalty_eligible")
