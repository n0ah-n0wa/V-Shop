"""Order status codecs, labels and transition rules (UI-agnostic)."""

from __future__ import annotations

from app.models.enums import OrderStatus
from app.services.localization import LocalizationService

_STATUS_LOCALE = {
    OrderStatus.NEW: "admin.order_status_new",
    OrderStatus.ACCEPTED: "admin.order_status_accepted",
    OrderStatus.SHIPPED: "admin.order_status_shipped",
    OrderStatus.COMPLETED: "admin.order_status_completed",
    OrderStatus.CANCELLED: "admin.order_status_cancelled",
}

# The action wording for moving an order *into* a status, e.g. "📦 Ship". Two share
# a row on the order card, so each stays short enough for a phone in every language.
_ACTION_LOCALE = {
    OrderStatus.NEW: "admin.order_action_reopen",
    OrderStatus.ACCEPTED: "admin.order_action_accept",
    OrderStatus.SHIPPED: "admin.order_action_ship",
    OrderStatus.COMPLETED: "admin.order_action_complete",
    OrderStatus.CANCELLED: "admin.order_action_cancel",
}

# Allowed moves. The happy path is linear:
#     New -> Accepted -> Shipped -> Completed
# Cancellation is possible from any state that is still active.
#
# Cancelled -> New exists as an UNDO for a mis-tapped cancellation: it returns
# the order to the start of the pipeline rather than to where it was, so the
# admin re-walks the steps deliberately.
#
# Completed stays terminal. Revenue reporting reads completed orders, and the
# customer has already been told the order finished; reopening it would rewrite
# history that other systems depend on.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.ACCEPTED, OrderStatus.CANCELLED}),
    OrderStatus.ACCEPTED: frozenset({OrderStatus.SHIPPED, OrderStatus.CANCELLED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.COMPLETED, OrderStatus.CANCELLED}),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.CANCELLED: frozenset({OrderStatus.NEW}),
}

# Stated explicitly rather than derived from the table: Cancelled has an undo
# path, but it is emphatically not an order still being fulfilled.
ACTIVE_STATUSES = frozenset({OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.SHIPPED})
# Statuses no move can leave.
TERMINAL_STATUSES = frozenset(
    status for status, targets in ALLOWED_TRANSITIONS.items() if not targets
)


def allowed_transitions(current: OrderStatus) -> tuple[OrderStatus, ...]:
    """Statuses reachable from ``current``, in lifecycle order."""
    targets = ALLOWED_TRANSITIONS.get(current, frozenset())
    return tuple(status for status in OrderStatus if status in targets)


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    """Whether moving ``current`` -> ``target`` is permitted."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL_STATUSES


def status_label(i18n: LocalizationService, status: OrderStatus | str) -> str:
    if isinstance(status, str):
        try:
            status = OrderStatus(status)
        except ValueError:
            return status
    key = _STATUS_LOCALE.get(status)
    return i18n.t(key) if key else str(status)


def status_action_label(i18n: LocalizationService, status: OrderStatus) -> str:
    """Button wording for moving an order into ``status``."""
    key = _ACTION_LOCALE.get(status)
    return i18n.t(key) if key else status_label(i18n, status)


def status_code(status: OrderStatus) -> str:
    return status.name.lower()


def status_from_code(code: str) -> OrderStatus | None:
    try:
        return OrderStatus[code.upper()]
    except KeyError:
        return None
