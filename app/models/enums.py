"""Domain enumerations used by ORM models."""

from enum import StrEnum


class LanguageCode(StrEnum):
    RU = "ru"
    EN = "en"
    DE = "de"
    UK = "uk"


class CityChoice(StrEnum):
    BERLIN = "berlin"
    DELIVERY = "delivery"


class DeliveryType(StrEnum):
    PICKUP = "pickup"
    COURIER = "courier"
    POSTAL = "postal"
    SERVICE = "service"


class PaymentMethod(StrEnum):
    """How the customer prefers to pay. Stored by value on the order."""

    CASH = "cash"
    CARD = "card"


class OrderStatus(StrEnum):
    NEW = "New"
    ACCEPTED = "Accepted"
    SHIPPED = "Shipped"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"


# --- loyalty ------------------------------------------------------------------
#
# The values below also appear as literals in CHECK constraints (models and the
# loyalty migration). Renaming one is a data migration, not a code change.


class LoyaltyTransactionType(StrEnum):
    """Why a stamp balance moved. One ledger row per event."""

    PURCHASE = "purchase"
    REFERRAL = "referral"
    ROULETTE = "roulette"
    REDEMPTION = "redemption"
    ADJUSTMENT = "adjustment"


class SpinGrantReason(StrEnum):
    """Why a customer was given a roulette spin."""

    INITIAL_PROMO = "initial_promo"
    PURCHASE_MILESTONE = "purchase_milestone"
    REFERRAL = "referral"


class RoulettePrizeType(StrEnum):
    STAMPS = "stamps"
    DISCOUNT_PERCENT = "discount_percent"
    FREE_BOTTLE = "free_bottle"


class RewardType(StrEnum):
    DISCOUNT_PERCENT = "discount_percent"
    FREE_BOTTLE = "free_bottle"


class RewardSource(StrEnum):
    STAMP_CARD = "stamp_card"
    ROULETTE = "roulette"


class RewardStatus(StrEnum):
    """Rewards never expire; a used reward stays used, even on a cancelled order."""

    AVAILABLE = "available"
    USED = "used"


class ReferralStatus(StrEnum):
    PENDING = "pending"
    QUALIFIED = "qualified"
