"""Render the statistics dashboard for Telegram.

Built for a phone screen. Three rules drive the layout:

* One figure per line, label first. Telegram has no columns — runs of spaces do
  not align anything once the labels differ in length, they only add noise.
* Every line short enough not to wrap. A wrapped line in a ranked list breaks
  the numbering down the left edge, which is what makes the list scannable.
* Notes and legends appear once, not once per section.

Counts are printed bare (``5``) rather than inflected ("5 orders"), which keeps
the Russian and Ukrainian catalogs free of plural-form tables — the legend and
the footnote say what is being counted.
"""

from __future__ import annotations

from decimal import Decimal

from app.repositories.statistics import ProductPopularity
from app.services.localization import LocalizationService
from app.services.statistics import OrderCounts, PeriodStats, ShopStatistics
from app.utils.html import e

DEFAULT_CURRENCY = "€"

# Long enough for a real product name, short enough that "1. <name> — 12" stays
# on one line at the default Telegram font on a narrow phone.
MAX_PRODUCT_NAME = 26


def group_digits(digits: str, separator: str) -> str:
    """Split a run of digits into thousands groups from the right."""
    groups: list[str] = []
    while len(digits) > 3:
        digits, group = digits[:-3], digits[-3:]
        groups.insert(0, group)
    groups.insert(0, digits)
    return separator.join(groups)


def format_amount(
    amount: Decimal,
    i18n: LocalizationService,
    currency: str = DEFAULT_CURRENCY,
    *,
    trim_zero_cents: bool = False,
) -> str:
    """
    Money in the reader's own convention.

    Separators and the position of the symbol both come from the locale
    catalog: English wants ``€1,234.56``, German ``1.234,56 €``. A single global
    format would read as a different number to half the audience.

    ``trim_zero_cents`` prints a whole amount without its cents — ``€20``
    rather than ``€20.00`` — for prose, where the zeros are only noise.
    """
    quantized = amount.quantize(Decimal("0.01"))
    whole, _, fraction = f"{abs(quantized):f}".partition(".")
    cents = (fraction or "00").ljust(2, "0")
    number = group_digits(whole, i18n.t("format.group_separator"))
    if not (trim_zero_cents and cents == "00"):
        number += i18n.t("format.decimal_separator") + cents
    sign = "-" if quantized < 0 else ""
    return i18n.t("format.money", amount=f"{sign}{number}", currency=currency)


def localized_popularity_name(row: ProductPopularity, language: str) -> str:
    """The product's name in the admin's language, falling back to English."""
    value = getattr(row, f"name_{language}", None)
    if isinstance(value, str) and value:
        return value
    return row.name_en


def shorten(name: str, i18n: LocalizationService, limit: int = MAX_PRODUCT_NAME) -> str:
    """
    Trim an over-long product name, then escape it.

    Trimming before escaping matters: cutting escaped text can slice an entity
    in half and leave ``&am`` in the message. Trimming at the last word boundary
    keeps the result readable rather than cutting mid-syllable.
    """
    if len(name) <= limit:
        return e(name)
    clipped = name[:limit].rstrip()
    head, space, _ = clipped.rpartition(" ")
    if space and len(head) >= limit // 2:
        clipped = head
    return i18n.t("admin.stats_name_truncated", name=e(clipped))


def _hint(i18n: LocalizationService, key: str) -> str:
    return i18n.t("admin.stats_hint", text=i18n.t(key))


def _orders_line(i18n: LocalizationService, period_key: str, counts: OrderCounts) -> str:
    return i18n.t(
        "admin.stats_orders_line",
        period=i18n.t(period_key),
        total=counts.total,
        completed=counts.completed,
        cancelled=counts.cancelled,
    )


def _revenue_line(
    i18n: LocalizationService, period_key: str, period: PeriodStats, currency: str
) -> str:
    return i18n.t(
        "admin.stats_revenue_line",
        period=i18n.t(period_key),
        amount=format_amount(period.revenue, i18n, currency),
    )


def _product_lines(
    i18n: LocalizationService,
    rows: list[ProductPopularity],
    empty_key: str,
) -> list[str]:
    """Ranked product lines, or a single placeholder when there is nothing to rank."""
    if not rows:
        return [i18n.t(empty_key)]
    return [
        i18n.t(
            "admin.stats_product_line",
            rank=rank,
            name=shorten(localized_popularity_name(row, i18n.language), i18n),
            count=row.order_count,
        )
        for rank, row in enumerate(rows, start=1)
    ]


def _general_line(i18n: LocalizationService, icon: str, label_key: str, value: int) -> str:
    return i18n.t("admin.stats_general_line", icon=icon, label=i18n.t(label_key), value=value)


def _section_header(i18n: LocalizationService, header_key: str, note_key: str) -> str:
    return i18n.t(
        "admin.stats_section_note",
        header=i18n.t(header_key),
        note=i18n.t(note_key),
    )


def format_statistics(
    stats: ShopStatistics,
    i18n: LocalizationService,
    currency: str = DEFAULT_CURRENCY,
) -> str:
    """The whole dashboard as one HTML message."""
    periods = (
        ("admin.stats_period_all", stats.all_time),
        ("admin.stats_period_current", stats.current_month),
        ("admin.stats_period_previous", stats.previous_month),
    )
    general = stats.general
    # No products at all is a different emptiness from products that never sold:
    # the first means "set up your catalog", the second "nothing has sold yet".
    sales_empty_key = (
        "admin.stats_empty_products" if general.products == 0 else "admin.stats_empty_sales"
    )

    blocks: list[list[str]] = [
        [
            i18n.t("admin.stats_title"),
            i18n.t(
                "admin.stats_period",
                month=f"{stats.bounds.current_start:%m.%Y}",
                timezone=e(stats.timezone),
            ),
        ],
        [
            i18n.t("admin.stats_general"),
            _general_line(i18n, "👤", "admin.stats_users", general.users),
            _general_line(i18n, "📂", "admin.stats_categories", general.categories),
            _general_line(i18n, "🏷", "admin.stats_subcategories", general.subcategories),
            _general_line(i18n, "📦", "admin.stats_products", general.products),
            _general_line(i18n, "🧾", "admin.stats_orders_total", general.orders),
        ],
        [
            i18n.t("admin.stats_orders"),
            # the legend sits directly above the lines that use its symbols
            _hint(i18n, "admin.stats_orders_legend"),
            *(_orders_line(i18n, key, period.orders) for key, period in periods),
        ],
        [
            _section_header(i18n, "admin.stats_revenue", "admin.stats_revenue_note"),
            *(_revenue_line(i18n, key, period, currency) for key, period in periods),
        ],
        [
            i18n.t("admin.stats_top"),
            *_product_lines(i18n, stats.most_ordered, sales_empty_key),
        ],
        [
            i18n.t("admin.stats_bottom"),
            *_product_lines(i18n, stats.least_ordered, "admin.stats_empty_products"),
        ],
    ]

    # One footnote for both lists rather than the same note in two headers. It
    # only earns its line when there is actually a ranking to explain.
    if stats.most_ordered or stats.least_ordered:
        blocks.append([_hint(i18n, "admin.stats_products_footnote")])

    # Blank line between sections, single line inside one — the shape that scans
    # fastest on a phone.
    return "\n\n".join("\n".join(block) for block in blocks)
