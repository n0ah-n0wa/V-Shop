"""loyalty foundation: accounts, stamp ledger, roulette, rewards, referrals

Persistence for the stamp card, the lucky roulette and the referral programme.

NON-DESTRUCTIVE BY DESIGN. Six new tables. No existing table, column or row is
altered or removed. The only writes are two INSERT-only backfills:

  * one ``loyalty_accounts`` row (0 stamps) for every existing user, so every
    customer has a valid account from the first deploy;
  * one ``initial_promo`` roulette grant for every existing user — the one-time
    welcome spin, granted unconditionally (owner decision).

Both backfills are guarded by ``NOT EXISTS`` and the initial grant is also
protected by a partial unique index, so no customer can ever hold two initial
spins. No historical order earns anything here: the programme applies only to
orders placed after it launches.

Every constraint, index and CHECK below mirrors ``app/models`` exactly;
``tests/test_loyalty_schema.py`` compares the CHECK texts, which ``alembic
check`` does not.

Ownership is checked by the database as well as the services: a spin, a reward
or a ledger row can only reference a grant, spin or reward of the same customer
(composite foreign keys onto ``(id, user_id)``).

Downgrade drops the six tables. While they hold only the backfill, nothing is
lost that a re-upgrade would not recreate identically. Once customers have used
the programme it would destroy their stamps, spins, rewards and referrals, so it
refuses unless told explicitly — take a ``pg_dump`` first::

    alembic -x allow_loyalty_data_loss=true downgrade f6b1d4e8a207

Revision ID: 3b9d6f2a8c14
Revises: f6b1d4e8a207
Create Date: 2026-09-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "3b9d6f2a8c14"
down_revision: str | None = "f6b1d4e8a207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


# Anything a customer did, as opposed to what the backfill created.
_CUSTOMER_ACTIVITY_SQL = (
    "SELECT"
    " (SELECT count(*) FROM loyalty_transactions)"
    " + (SELECT count(*) FROM roulette_spins)"
    " + (SELECT count(*) FROM user_rewards)"
    " + (SELECT count(*) FROM referrals)"
    " + (SELECT count(*) FROM roulette_spin_grants"
    " WHERE reason <> 'initial_promo' OR consumed_at IS NOT NULL)"
    " + (SELECT count(*) FROM loyalty_accounts WHERE referral_code IS NOT NULL)"
)


def _refuse_to_destroy_customer_activity() -> None:
    """Stop a downgrade that would delete what customers earned or were given."""
    if context.is_offline_mode():
        return  # emitting SQL for review: there is no database to inspect
    flag = context.get_x_argument(as_dictionary=True).get("allow_loyalty_data_loss", "")
    allowed = str(flag).strip().lower() in {"1", "true", "yes"}
    activity = int(op.get_bind().execute(sa.text(_CUSTOMER_ACTIVITY_SQL)).scalar() or 0)
    if activity and not allowed:
        raise RuntimeError(
            f"Refusing to downgrade 3b9d6f2a8c14: {activity} loyalty record(s) created "
            "by customers would be destroyed. Take a pg_dump first, then re-run with "
            "`alembic -x allow_loyalty_data_loss=true downgrade <revision>`."
        )


def upgrade() -> None:
    # ----------------------------------------------------------------- referrals
    op.create_table(
        "referrals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("referrer_user_id", sa.Integer(), nullable=False),
        sa.Column("referred_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "qualified", name="referral_status", native_enum=False, length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("qualifying_order_id", sa.Integer(), nullable=True),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "referrer_user_id <> referred_user_id",
            name="ck_referrals_not_self",
        ),
        sa.CheckConstraint(
            "((status = 'qualified') = (qualifying_order_id IS NOT NULL))"
            " AND ((status = 'qualified') = (qualified_at IS NOT NULL))",
            name="ck_referrals_qualified_state",
        ),
        sa.ForeignKeyConstraint(["referrer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["referred_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["qualifying_order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("referred_user_id", name="uq_referrals_referred_user_id"),
        sa.UniqueConstraint("qualifying_order_id", name="uq_referrals_qualifying_order_id"),
    )
    op.create_index(
        op.f("ix_referrals_referrer_user_id"),
        "referrals",
        ["referrer_user_id"],
        unique=False,
    )

    # ------------------------------------------------------ roulette_spin_grants
    op.create_table(
        "roulette_spin_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "initial_promo",
                "purchase_milestone",
                "referral",
                name="spin_grant_reason",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("referral_id", sa.Integer(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "((reason = 'purchase_milestone') = (order_id IS NOT NULL))"
            " AND ((reason = 'referral') = (referral_id IS NOT NULL))",
            name="ck_roulette_spin_grants_source_matches_reason",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["referral_id"], ["referrals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_roulette_spin_grants_order_id"),
        sa.UniqueConstraint(
            "referral_id",
            "user_id",
            name="uq_roulette_spin_grants_referral_id_user_id",
        ),
        sa.UniqueConstraint("id", "user_id", name="uq_roulette_spin_grants_id_user_id"),
    )
    op.create_index(
        "ix_roulette_spin_grants_user_id_consumed_at",
        "roulette_spin_grants",
        ["user_id", "consumed_at"],
        unique=False,
    )
    op.create_index(
        "ix_roulette_spin_grants_initial_promo_user_id",
        "roulette_spin_grants",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("reason = 'initial_promo'"),
    )

    # ------------------------------------------------------------ roulette_spins
    op.create_table(
        "roulette_spins",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=False),
        sa.Column("prize_code", sa.String(length=32), nullable=False),
        sa.Column(
            "prize_type",
            sa.Enum(
                "stamps",
                "discount_percent",
                "free_bottle",
                name="roulette_prize_type",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("prize_value", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint("prize_value > 0", name="ck_roulette_spins_prize_value_positive"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["grant_id", "user_id"],
            ["roulette_spin_grants.id", "roulette_spin_grants.user_id"],
            ondelete="RESTRICT",
            name="fk_roulette_spins_grant_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("grant_id", name="uq_roulette_spins_grant_id"),
        sa.UniqueConstraint("id", "user_id", name="uq_roulette_spins_id_user_id"),
    )
    op.create_index(op.f("ix_roulette_spins_user_id"), "roulette_spins", ["user_id"], unique=False)

    # -------------------------------------------------------------- user_rewards
    op.create_table(
        "user_rewards",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "discount_percent",
                "free_bottle",
                name="reward_type",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("max_item_price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column(
            "source",
            sa.Enum("stamp_card", "roulette", name="reward_source", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("available", "used", name="reward_status", native_enum=False, length=32),
            server_default="available",
            nullable=False,
        ),
        sa.Column("spin_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "value > 0"
            " AND (kind <> 'discount_percent'"
            " OR (value <= 100 AND max_item_price IS NULL))"
            " AND (kind <> 'free_bottle'"
            " OR (value = 1 AND max_item_price IS NOT NULL AND max_item_price > 0))",
            name="ck_user_rewards_terms",
        ),
        sa.CheckConstraint(
            "((status = 'used') = (order_id IS NOT NULL))"
            " AND ((status = 'used') = (used_at IS NOT NULL))",
            name="ck_user_rewards_used_state",
        ),
        sa.CheckConstraint(
            "(source = 'roulette') = (spin_id IS NOT NULL)",
            name="ck_user_rewards_source_matches_spin",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["spin_id", "user_id"],
            ["roulette_spins.id", "roulette_spins.user_id"],
            ondelete="RESTRICT",
            name="fk_user_rewards_spin_owner",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spin_id", name="uq_user_rewards_spin_id"),
        sa.UniqueConstraint("order_id", name="uq_user_rewards_order_id"),
        sa.UniqueConstraint("id", "user_id", name="uq_user_rewards_id_user_id"),
    )
    op.create_index(
        "ix_user_rewards_user_id_status",
        "user_rewards",
        ["user_id", "status"],
        unique=False,
    )

    # ---------------------------------------------------------- loyalty_accounts
    op.create_table(
        "loyalty_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("stamp_balance", sa.Integer(), server_default="0", nullable=False),
        sa.Column("qualifying_purchase_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("referral_code", sa.String(length=32), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "stamp_balance >= 0",
            name="ck_loyalty_accounts_stamp_balance_non_negative",
        ),
        sa.CheckConstraint(
            "qualifying_purchase_count >= 0",
            name="ck_loyalty_accounts_purchase_count_non_negative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("referral_code", name="uq_loyalty_accounts_referral_code"),
    )
    op.create_index(
        op.f("ix_loyalty_accounts_user_id"),
        "loyalty_accounts",
        ["user_id"],
        unique=True,
    )

    # ------------------------------------------------------ loyalty_transactions
    op.create_table(
        "loyalty_transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "purchase",
                "referral",
                "roulette",
                "redemption",
                "adjustment",
                name="loyalty_transaction_type",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("referral_id", sa.Integer(), nullable=True),
        sa.Column("spin_id", sa.Integer(), nullable=True),
        sa.Column("reward_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "(kind <> 'purchase' OR amount >= 0)"
            " AND (kind NOT IN ('referral', 'roulette') OR amount > 0)"
            " AND (kind <> 'redemption' OR amount < 0)"
            " AND (kind = 'purchase' OR amount <> 0)",
            name="ck_loyalty_transactions_amount_sign",
        ),
        sa.CheckConstraint(
            "((kind = 'purchase') = (order_id IS NOT NULL))"
            " AND ((kind = 'referral') = (referral_id IS NOT NULL))"
            " AND ((kind = 'roulette') = (spin_id IS NOT NULL))"
            " AND ((kind = 'redemption') = (reward_id IS NOT NULL))",
            name="ck_loyalty_transactions_source_matches_kind",
        ),
        sa.CheckConstraint(
            "kind <> 'adjustment' OR note IS NOT NULL",
            name="ck_loyalty_transactions_adjustment_has_note",
        ),
        sa.CheckConstraint(
            "balance_after >= 0",
            name="ck_loyalty_transactions_balance_after_non_negative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["referral_id"], ["referrals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["spin_id", "user_id"],
            ["roulette_spins.id", "roulette_spins.user_id"],
            ondelete="RESTRICT",
            name="fk_loyalty_transactions_spin_owner",
        ),
        sa.ForeignKeyConstraint(
            ["reward_id", "user_id"],
            ["user_rewards.id", "user_rewards.user_id"],
            ondelete="RESTRICT",
            name="fk_loyalty_transactions_reward_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_loyalty_transactions_order_id"),
        sa.UniqueConstraint(
            "referral_id",
            "user_id",
            name="uq_loyalty_transactions_referral_id_user_id",
        ),
        sa.UniqueConstraint("spin_id", name="uq_loyalty_transactions_spin_id"),
        sa.UniqueConstraint("reward_id", name="uq_loyalty_transactions_reward_id"),
    )
    op.create_index(
        "ix_loyalty_transactions_user_id_id",
        "loyalty_transactions",
        ["user_id", "id"],
        unique=False,
    )

    # ------------------------------------------------------------------ backfill
    # Every existing customer gets an account and their one welcome spin.
    op.execute(
        "INSERT INTO loyalty_accounts (user_id, stamp_balance, qualifying_purchase_count) "
        "SELECT u.id, 0, 0 FROM users u "
        "WHERE NOT EXISTS (SELECT 1 FROM loyalty_accounts a WHERE a.user_id = u.id)"
    )
    op.execute(
        "INSERT INTO roulette_spin_grants (user_id, reason) "
        "SELECT u.id, 'initial_promo' FROM users u "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM roulette_spin_grants g "
        "WHERE g.user_id = u.id AND g.reason = 'initial_promo')"
    )


def downgrade() -> None:
    # Destroys all loyalty data; existing users, orders and catalog are untouched.
    _refuse_to_destroy_customer_activity()
    op.drop_index("ix_loyalty_transactions_user_id_id", table_name="loyalty_transactions")
    op.drop_table("loyalty_transactions")
    op.drop_index(op.f("ix_loyalty_accounts_user_id"), table_name="loyalty_accounts")
    op.drop_table("loyalty_accounts")
    op.drop_index("ix_user_rewards_user_id_status", table_name="user_rewards")
    op.drop_table("user_rewards")
    op.drop_index(op.f("ix_roulette_spins_user_id"), table_name="roulette_spins")
    op.drop_table("roulette_spins")
    op.drop_index(
        "ix_roulette_spin_grants_initial_promo_user_id",
        table_name="roulette_spin_grants",
    )
    op.drop_index(
        "ix_roulette_spin_grants_user_id_consumed_at",
        table_name="roulette_spin_grants",
    )
    op.drop_table("roulette_spin_grants")
    op.drop_index(op.f("ix_referrals_referrer_user_id"), table_name="referrals")
    op.drop_table("referrals")
