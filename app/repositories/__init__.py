"""Repository layer package (data access)."""

from app.repositories.base import BaseRepository
from app.repositories.cart import CartRepository
from app.repositories.cart_item import CartItemRepository
from app.repositories.category import CategoryRepository
from app.repositories.loyalty_account import LoyaltyAccountRepository
from app.repositories.loyalty_transaction import LoyaltyTransactionRepository
from app.repositories.order import OrderRepository
from app.repositories.order_item import OrderItemRepository
from app.repositories.product import ProductRepository
from app.repositories.referral import ReferralRepository
from app.repositories.roulette_spin import RouletteSpinRepository
from app.repositories.roulette_spin_grant import RouletteSpinGrantRepository
from app.repositories.subcategory import SubcategoryRepository
from app.repositories.user import UserRepository
from app.repositories.user_reward import UserRewardRepository

__all__ = [
    "BaseRepository",
    "CartItemRepository",
    "CartRepository",
    "CategoryRepository",
    "LoyaltyAccountRepository",
    "LoyaltyTransactionRepository",
    "OrderItemRepository",
    "OrderRepository",
    "ProductRepository",
    "ReferralRepository",
    "RouletteSpinGrantRepository",
    "RouletteSpinRepository",
    "SubcategoryRepository",
    "UserRepository",
    "UserRewardRepository",
]
