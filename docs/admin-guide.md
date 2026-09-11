# Admin guide

Admin features are available only to Telegram user IDs listed in `ADMIN_IDS`.

## Access

1. Ensure your numeric Telegram ID is in `.env` → `ADMIN_IDS`.
2. Restart the bot if you changed env vars.
3. Send `/admin` to the bot.

Authorized users get the admin reply keyboard:

| Button | Section |
|---|---|
| Products | Add / manage catalog products |
| Categories | Create / rename / delete / reorder |
| Orders | View, search, change status |
| Broadcast | Message all registered users |
| Settings | Admin settings entry |

Non-admins who send `/admin` receive an access-denied message and never see admin controls.

While a wizard is active (add product, rename category, broadcast, etc.), tapping another admin menu button is blocked until you **Cancel** or finish the flow.

---

## Products

### Add product

1. **Products** → **Add product**
2. Send a **photo** (required for new products in the wizard)
3. Enter names: RU → EN → DE
4. Enter descriptions: RU → EN → DE
5. Pick a **category** (create categories first if the list is empty)
6. Enter flavor, volume, nicotine strength, price
7. Review the preview → **Confirm**

Prices use decimal format (`12.50`). Scientific notation is rejected.

### Manage products

1. **Products** → **Manage** (paginated list)
2. Open a product card

Actions:

- **Edit** — full edit wizard; **Skip** keeps the current value for a step
- **Edit price** / **Edit description** — focused flows
- **Enable** / **Disable** — catalog visibility (`is_active`)
- **Delete** — confirmation required; blocked if the product appears in any order history

Product images use Telegram `file_id` from the uploaded photo.

---

## Statistics

`📊 Statistics` on the admin panel. Private admin chats only — the button is
absent from every customer keyboard, the router sits behind `IsAdmin` plus
`AdminOnlyMiddleware`, and group traffic is dropped before it reaches any
handler.

The dashboard is one message:

| Section | Contents |
|---|---|
| General | users, categories, subcategories, products, total orders |
| Orders | all time / this month / last month, each total ✅ completed ❌ cancelled |
| Revenue | all time / this month / last month, **completed orders only** |
| Products | top 3 most ordered and top 3 least ordered |

Product rankings count **distinct completed orders** containing the product, not
units sold: an order holding five of an item counts once. Only products that are
on sale are ranked (see [What "on sale" means](#what-on-sale-means)), and a
product with zero completed orders qualifies for the bottom list — that is what
the list is for.

Month boundaries follow `APP_TIMEZONE` (default `Europe/Berlin`), not the server
clock, so an order placed at 00:30 local on the 1st belongs to the new month.
The header shows the month and zone the figures were cut with.

Money is punctuated the way the reader's language writes numbers — `€1,234.56`
in English, `1.234,56 €` in German, `1 234,56 €` in Russian and Ukrainian. The
symbol itself comes from `CURRENCY_SYMBOL`.

Product names longer than 26 characters are trimmed at the nearest word with an
ellipsis, so a ranked list stays one line per entry. Without that a single long
name wraps to three rows and the numbering stops reading as a list.

`🔄 Refresh` redraws in place. If nothing has changed since the last tap the
message stays as it is — Telegram rejects an unchanged edit, and that is not an
error.

Empty states are distinct on purpose: an empty catalog says there are no
products, while a stocked catalog with no completed sales says nothing has sold
yet.

### What "on sale" means

A product is on sale only when **all three** are active: the product, its
category, and — if it has one — its subcategory. Disabling a category or a
subcategory therefore takes every product under it off the shelf without
touching the product rows, and re-enabling puts them straight back.

The rule is defined once, in `app/repositories/visibility.py`, and applies
everywhere the question is asked: catalog browsing, the checkout guard (an item
that went off sale while sitting in a cart is refused), and the statistics
top/bottom product rankings. A product a customer cannot buy never appears in
the best- or worst-seller lists, because "not selling" and "not on sale" are
different problems.

Products created before the category → subcategory → product hierarchy carry no
subcategory. They are judged on their category alone.

---

## Categories

1. Open **Categories**
2. **Create** — enter a unique-enough display name (sorted by `sort_order`)
3. Open a category to:
   - **Rename**
   - **Delete** (blocked if it still has products)
   - **Move up / down** — changes display order in the customer catalog

Create at least one category before adding products.

---

## Orders

1. Open **Orders**
2. View **New** or **Completed** lists (paginated)
3. Open an order for the full card (customer, items, totals, contacts). An order
   paid partly with a loyalty reward shows a **Reward used** line under the items:
   a free bottle is the item listed at 0.00, a discount is already off the total —
   charge the total shown
4. Completing an order books the customer's stamps, any roulette spin it earns and
   a first order's referral bonus automatically — there is nothing to do by hand
4. Change status: **New** → **Accepted** → **Completed**, or **Cancelled**
5. **Search** by order ID, customer name, or phone

New orders also notify:

- `MANAGER_CHAT_ID` (group or private)
- Each ID in `ADMIN_IDS` (private), excluding duplicates

---

## Broadcast

1. Open **Broadcast**
2. Compose **text** and/or send a **photo** (caption optional)
3. Review the preview (recipient count shown)
4. **Confirm** to send

Progress updates appear as the fan-out runs. Soft rate limiting and `RetryAfter` handling reduce Telegram flood errors. Failed recipient IDs are summarized at the end.

Do not double-tap Confirm; the bot serializes broadcast confirms per admin.

---

## Customer-facing reminder

After catalog/product changes:

- Disabled products disappear from the customer catalog
- Category order matches admin reorder
- Cart lines for deleted products are removed (delete only succeeds when not used in orders)

---

## Operational tips

- Keep `ADMIN_IDS` small and trusted — admins can broadcast to every user.
- Use a dedicated manager group for `MANAGER_CHAT_ID` so order alerts are shared.
- If the bot restarts mid-wizard, ask the admin to `/admin` again and restart the flow (FSM is in-memory).
- For TLS interception on a corporate network during local testing only, see `TELEGRAM_SSL_VERIFY` in [Configuration](configuration.md).
