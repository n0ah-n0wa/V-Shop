"""reward redemption record: what a used reward was worth, and what it paid for

NON-DESTRUCTIVE. Two nullable columns on the loyalty table ``user_rewards`` — no
catalog, order or user data is touched:

  * ``discount_amount`` — the value the reward took off the order it was used on;
  * ``redeemed_product_id`` — for a free bottle, the product that was made free.

Order lines keep only the price charged (a free bottle is a €0 line), so without
these the value given away would be unrecoverable once the product is repriced.

A CHECK ties both to the reward's state: a used reward always records its
discount, a used free bottle always names its product, and neither is set on a
reward that has not been used. No reward can have been used before this revision
— no redemption path existed — so existing rows satisfy it; if one somehow did
not, the upgrade fails as a whole and changes nothing.

Downgrade drops both columns and with them the redemption record, so it refuses
while used rewards exist unless told explicitly::

    alembic -x allow_loyalty_data_loss=true downgrade 8e4c1a7b2d95

Revision ID: c5d2e8f1a6b3
Revises: 8e4c1a7b2d95
Create Date: 2026-09-10 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "c5d2e8f1a6b3"
down_revision: str | None = "8e4c1a7b2d95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_to_lose_redemption_records() -> None:
    """Stop a downgrade that would forget what redeemed rewards were worth."""
    if context.is_offline_mode():
        return  # emitting SQL for review: there is no database to inspect
    flag = context.get_x_argument(as_dictionary=True).get("allow_loyalty_data_loss", "")
    allowed = str(flag).strip().lower() in {"1", "true", "yes"}
    used = int(
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM user_rewards WHERE status = 'used'"))
        .scalar()
        or 0
    )
    if used and not allowed:
        raise RuntimeError(
            f"Refusing to downgrade c5d2e8f1a6b3: the redemption record of {used} used "
            "reward(s) would be lost. Take a pg_dump first, then re-run with "
            "`alembic -x allow_loyalty_data_loss=true downgrade <revision>`."
        )


def upgrade() -> None:
    op.add_column(
        "user_rewards",
        sa.Column("discount_amount", sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.add_column(
        "user_rewards",
        sa.Column("redeemed_product_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_rewards_redeemed_product",
        "user_rewards",
        "products",
        ["redeemed_product_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_user_rewards_redemption_record",
        "user_rewards",
        "((status = 'used') = (discount_amount IS NOT NULL))"
        " AND ((kind = 'free_bottle' AND status = 'used') = (redeemed_product_id IS NOT NULL))"
        " AND (discount_amount IS NULL OR discount_amount >= 0)",
    )


def downgrade() -> None:
    _refuse_to_lose_redemption_records()
    op.drop_constraint("ck_user_rewards_redemption_record", "user_rewards", type_="check")
    op.drop_constraint("fk_user_rewards_redeemed_product", "user_rewards", type_="foreignkey")
    op.drop_column("user_rewards", "redeemed_product_id")
    op.drop_column("user_rewards", "discount_amount")
