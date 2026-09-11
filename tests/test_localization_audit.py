"""Standing localization audit — keeps user-facing text in the locale system.

These tests encode the result of the manual audit so regressions are caught
automatically:

* every locale key referenced in code must exist (a missing key renders the raw
  key string to the user, silently);
* every locale catalog must carry the same keys;
* handlers and keyboards must not pass literal strings to Telegram.

Deliberate exceptions are listed in :data:`DOCUMENTED_EXCEPTIONS` and must carry
an ``INTENTIONALLY NOT LOCALIZED`` marker in the source.
"""

from __future__ import annotations

import ast
import pathlib
import re

from app.utils.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, locale_keys

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Modules allowed to build user-visible text outside the locale system.
DOCUMENTED_EXCEPTIONS = {
    "services/notification.py": "manager/ops order alert — single shared chat",
    "utils/labels.py": "English city/delivery labels for the ops alert",
}

# Telegram-delivering call names whose text must never be a literal.
DELIVERY_CALLS = {
    "answer",
    "answer_photo",
    "reply",
    "edit_text",
    "edit_caption",
    "send_message",
    "send_photo",
}
TEXT_KWARGS = {"text", "caption"}


def _py_files(*subdirs: str) -> list[pathlib.Path]:
    roots = [APP / d for d in subdirs] if subdirs else [APP]
    return sorted(f for root in roots for f in root.rglob("*.py"))


def test_every_referenced_locale_key_exists() -> None:
    """A typo'd key would render as raw text (e.g. 'chekout.ask_name') to a user."""
    catalog = locale_keys(DEFAULT_LANGUAGE)
    call = re.compile(r'\.(?:t|get|n)\(\s*["\']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)["\']')
    filt = re.compile(r'LocalizedText\(\s*["\']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)["\']')

    missing: dict[str, str] = {}
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        for key in set(call.findall(text)) | set(filt.findall(text)):
            if key not in catalog:
                missing[key] = str(path.relative_to(APP.parent))
    assert not missing, f"locale keys referenced but not defined: {missing}"


def test_all_catalogs_carry_the_same_keys() -> None:
    base = locale_keys(DEFAULT_LANGUAGE)
    for code in SUPPORTED_LANGUAGES:
        keys = locale_keys(code)
        assert keys == base, (
            f"{code} out of sync: missing={sorted(base - keys)} extra={sorted(keys - base)}"
        )


def test_handlers_and_keyboards_send_no_literal_text() -> None:
    """Telegram-bound text must come from i18n, never a string literal."""
    offenders: list[str] = []
    for path in _py_files("handlers", "keyboards"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) not in DELIVERY_CALLS:
                continue
            candidates = list(node.args[:1])
            candidates += [kw.value for kw in node.keywords if kw.arg in TEXT_KWARGS]
            for value in candidates:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    rel = path.relative_to(APP.parent)
                    offenders.append(f"{rel}:{node.lineno} {value.value[:40]!r}")
    assert not offenders, "hardcoded user-facing text: " + "; ".join(offenders)


def test_keyboard_button_labels_come_from_i18n() -> None:
    """Button captions must be translated or be database content, never literals.

    Scans the whole app tree, not just ``keyboards/``, so a button built inside a
    handler cannot slip past.
    """
    offenders: list[str] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) not in {
                "InlineKeyboardButton",
                "KeyboardButton",
            }:
                continue
            for kw in node.keywords:
                if kw.arg == "text" and isinstance(kw.value, ast.Constant):
                    rel = path.relative_to(APP.parent)
                    offenders.append(f"{rel}:{node.lineno} {kw.value.value!r}")
    assert not offenders, "hardcoded button labels: " + "; ".join(offenders)


def test_documented_exceptions_are_actually_marked() -> None:
    """Anything exempted must say so in its own source, discoverably."""
    for rel, reason in DOCUMENTED_EXCEPTIONS.items():
        path = APP / rel
        assert path.exists(), f"{rel} listed as an exception but does not exist"
        source = path.read_text(encoding="utf-8")
        assert "INTENTIONALLY NOT LOCALIZED" in source, (
            f"{rel} is exempted ({reason}) but carries no 'INTENTIONALLY NOT LOCALIZED' marker"
        )


# Where customer-facing text is assembled: every word must come from a catalog.
USER_FACING_TREES = ("handlers", "keyboards", "services", "errors", "utils")
# Two words of letters in a row: prose, not a key, a callback or a format string.
PROSE = re.compile(r"[^\W\d_]{2,}\s+[^\W\d_]{2,}")
# Literals that read like prose but never reach a customer.
NOT_SHOWN = {
    "message is not modified": "Telegram's own error text, matched to skip a no-op edit",
    "not modified": "the same Telegram error text, matched by the statistics screen",
    "friend joined referral_id=": "log context handed to _give_up, which only logs it",
    "rewards paid order_id=": "log context handed to _give_up, which only logs it",
    "V-Shop reviews": "the reviews-group invite link's name, listed to group admins only",
}
LOG_OWNERS = {"logger", "logging", "log"}


def _developer_text(tree: ast.AST) -> set[int]:
    """Ids of string nodes only developers read: docstrings, log lines, exception messages."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            owner = getattr(getattr(func, "value", None), "id", "")
            # An exception's message, raised or passed up to Exception.__init__.
            if owner in LOG_OWNERS or name.endswith(("Error", "Exception")) or name == "__init__":
                skip.update(id(sub) for sub in ast.walk(node))
    return skip


def test_user_facing_modules_build_no_prose_from_literals() -> None:
    """
    Handlers, keyboards, services, error paths and display helpers: no sentence
    is written in code. The manager alert is the documented exception.
    """
    offenders: list[str] = []
    for path in _py_files(*USER_FACING_TREES):
        rel = path.relative_to(APP).as_posix()
        if rel in DOCUMENTED_EXCEPTIONS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _developer_text(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in skip or node.value in NOT_SHOWN:
                continue
            if PROSE.search(node.value):
                offenders.append(f"{rel}:{node.lineno} {node.value[:50]!r}")
    assert not offenders, "prose written in code, not in a catalog: " + "; ".join(offenders)


def test_no_undocumented_module_builds_user_prose() -> None:
    """Guard against a new module quietly growing hardcoded UI text."""
    label = re.compile(r'["\'][^"\']*<b>[^"\']*</b>[^"\']*["\']')
    offenders: list[str] = []
    for path in _py_files():
        rel = path.relative_to(APP).as_posix()
        if rel in DOCUMENTED_EXCEPTIONS:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", "*")) or ".t(" in line:
                continue
            if label.search(line):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, "HTML-formatted prose built outside the locale system: " + ", ".join(
        offenders
    )
