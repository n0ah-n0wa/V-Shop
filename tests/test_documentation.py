"""
Documentation that cannot silently drift from the code.

Written after a review found `docs/architecture.md` listing a four-item
middleware chain that had five items in it — the private-chat gate, the one that
enforces group isolation, was missing entirely. Prose has no compiler, so the
facts a reader would act on are pinned here instead.

Only *checkable* facts are asserted: enum values, setting names, table names,
migration revisions, and the middleware order. Explanations are left to the
prose, where they belong.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

import app.models  # noqa: F401  (populates the metadata)
from app.config import Settings
from app.database.base import Base
from app.models.enums import CityChoice, LanguageCode, OrderStatus, PaymentMethod
from app.utils.order_status import ALLOWED_TRANSITIONS

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]
TEXT = {path.name: path.read_text(encoding="utf-8") for path in DOCS}
ALL_DOCS = "\n".join(TEXT.values())


def test_the_docs_directory_is_where_we_think_it_is() -> None:
    assert len(DOCS) >= 6, f"expected the doc set, found {[d.name for d in DOCS]}"


@pytest.mark.parametrize("name", sorted(Settings.model_fields))
def test_every_setting_is_documented(name: str) -> None:
    """
    An undocumented setting is one an operator cannot know to set.

    Two of these — the reviews group variables — were live in the code and absent
    from the docs for the whole feature cycle.
    """
    assert f"`{name.upper()}`" in ALL_DOCS, (
        f"{name.upper()} is a real setting but no document mentions it"
    )


@pytest.mark.parametrize("status", list(OrderStatus))
def test_every_order_status_is_documented(status: OrderStatus) -> None:
    assert status.value in ALL_DOCS, f"order status {status.value!r} is undocumented"


@pytest.mark.parametrize("method", list(PaymentMethod))
def test_every_payment_method_is_documented(method: PaymentMethod) -> None:
    assert f"`{method.value}`" in ALL_DOCS, f"payment method {method.value!r} is undocumented"


@pytest.mark.parametrize("language", list(LanguageCode))
def test_every_supported_language_is_documented(language: LanguageCode) -> None:
    assert f"`{language.value}`" in ALL_DOCS, (
        f"language {language.value!r} is supported but undocumented"
    )


@pytest.mark.parametrize("city", list(CityChoice))
def test_every_city_is_documented(city: CityChoice) -> None:
    assert f"`{city.value}`" in ALL_DOCS


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_every_table_is_in_the_schema_doc(table: str) -> None:
    assert table in TEXT["database-schema.md"], (
        f"table {table!r} exists but database-schema.md does not mention it"
    )


def test_every_migration_is_listed_in_the_deployment_doc() -> None:
    """
    An operator deciding whether a downgrade is safe reads this table.

    A migration missing from it is one they would have to open the source to
    reason about, mid-incident.
    """
    revisions = set()
    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        match = re.search(
            r"revision(?:: str)? = ['\"]([a-f0-9]+)['\"]",
            path.read_text(encoding="utf-8"),
        )
        if match:
            revisions.add(match.group(1))

    missing = sorted(r for r in revisions if r not in TEXT["deployment.md"])
    assert missing == [], f"migrations absent from deployment.md: {missing}"


def test_the_documented_middleware_order_matches_the_code() -> None:
    """
    The regression that prompted this file.

    The order decides whether a group update can open a database session or
    provoke a reply into a group, so a stale list here is a security document
    describing protection the code does not provide.
    """
    source = (ROOT / "app" / "middlewares" / "__init__.py").read_text(encoding="utf-8")
    registered = re.findall(r"dispatcher\.update\.outer_middleware\((\w+)\)", source)
    variable_to_label = {
        "logging_mw": "Logging",
        "private_chat": "PrivateChat",
        "error_mw": "Error handling",
        "database": "Database",
        "localization": "Localization",
    }
    actual = [variable_to_label[name] for name in registered]

    numbered = re.findall(r"^\d+\.\s+\*\*(.+?)\*\*", TEXT["architecture.md"], re.MULTILINE)
    documented = numbered[: len(actual)]
    # The list must end where the code's does: a middleware dropped from the code
    # but still documented would otherwise pass as a matching prefix.
    beyond = [
        label
        for label in numbered[len(actual) : len(actual) + 1]
        if label in variable_to_label.values()
    ]

    assert documented == actual, (
        f"architecture.md lists {documented} but the code registers {actual}"
    )
    assert beyond == [], f"architecture.md also lists {beyond}, which the code does not register"


def test_the_documented_status_transitions_match_the_code() -> None:
    """Terminal states and the cancel-undo are the parts people rely on."""
    schema = TEXT["database-schema.md"]

    terminal = sorted(
        status.value for status, allowed in ALLOWED_TRANSITIONS.items() if not allowed
    )
    assert terminal == ["Completed"], "the terminal state changed"
    assert "terminal" in schema.lower(), "database-schema.md no longer marks it"

    undo = OrderStatus.NEW in ALLOWED_TRANSITIONS[OrderStatus.CANCELLED]
    assert undo, "Cancelled -> New was removed"
    assert "Cancelled → New" in schema or "Cancelled -> New" in schema, (
        "the cancel-undo is no longer documented"
    )


def test_the_compose_project_and_volume_names_are_documented() -> None:
    """
    The two lines that fix the original catalog-loss incident.

    If either is renamed without the docs following, the upgrade instructions
    send an operator to the wrong volume.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    project = re.search(r"^name:\s*(\S+)", compose, re.MULTILINE)
    volume = re.search(r"name:\s*\"?\$\{POSTGRES_VOLUME_NAME:\?", compose)

    assert project and volume, "docker-compose.yml no longer pins both names"
    assert project.group(1) in ALL_DOCS, "the Compose project name is undocumented"
    assert "POSTGRES_VOLUME_NAME" in ALL_DOCS, "the volume setting is undocumented"


def test_compose_never_creates_or_defaults_the_database_volume() -> None:
    """
    The catalog-loss incident, closed at its source.

    A volume Compose may create — or one with a default name — lets a redeploy
    attach the bot to a new, empty database without a word: migrations apply,
    the bot starts healthy, the catalog is gone. The volume must be `external`,
    and its name required, so a missing or wrong name stops `docker compose up`.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^volumes:\n  pgdata:\n(.*?)(?=^\S|\Z)", compose)

    assert block, "docker-compose.yml no longer declares the pgdata volume"
    settings = "\n".join(
        line.strip() for line in block.group(1).splitlines() if not line.strip().startswith("#")
    )
    assert "external: true" in settings, "the database volume must be external"
    assert re.search(r"\$\{POSTGRES_VOLUME_NAME:\?[^}]+\}", settings), (
        "POSTGRES_VOLUME_NAME must be required (`:?`), never defaulted"
    )
    assert ":-" not in settings, "the database volume name must have no default"


def test_destructive_operations_are_named_as_such() -> None:
    """An operator must be able to find this list by searching for the command."""
    deployment = TEXT["deployment.md"]
    for command in ("docker compose down -v", "docker volume prune", "docker system prune"):
        assert command in deployment, f"{command!r} is not called out as destructive"
    assert "## Destructive operations" in deployment


def test_the_update_and_recovery_procedures_exist() -> None:
    deployment = TEXT["deployment.md"]
    assert "pg_dump" in deployment, "no backup procedure"
    assert "pg_restore" in deployment, "no restore procedure"
    assert "## Recovery" in deployment, "no recovery runbook"
    assert "system_identifier" in deployment, (
        "the deploy verification step is missing its key signal"
    )


def test_no_document_references_a_file_that_does_not_exist() -> None:
    """A link into the source is a promise the file is still there."""
    referenced = {
        match.group(1)
        for text in TEXT.values()
        for match in re.finditer(r"`((?:app|tests|alembic|docs)/[\w/]+\.py)`", text)
    }
    missing = sorted(path for path in referenced if not (ROOT / path).exists())
    assert missing == [], f"documentation points at files that do not exist: {missing}"


@pytest.mark.parametrize("table_name", sorted(Base.metadata.tables))
def test_documented_index_lists_match_the_schema(table_name: str) -> None:
    """
    An index list is the kind of detail that rots quietly.

    Two were already wrong when this was written: `orders` still claimed a
    single-column `status` index that had been dropped, and `products` was
    missing both of its `is_active` composites. Only tables whose section
    actually states an "Indexes:" line are checked — silence is allowed, a wrong
    answer is not.
    """
    schema = TEXT["database-schema.md"]
    section = re.search(rf"### `{table_name}`(.*?)(?=\n### |\n## |\Z)", schema, re.DOTALL)
    if section is None:
        pytest.skip(f"{table_name} has no section of its own")
    stated = re.search(r"Indexes:(.*?)(?:\n\n|\Z)", section.group(1), re.DOTALL)
    if stated is None:
        pytest.skip(f"{table_name} does not document its indexes")

    documented = " ".join(stated.group(1).split())
    unmentioned = []
    for index in Base.metadata.tables[table_name].indexes:
        columns = [column.name for column in index.columns]
        if len(columns) == 1:
            found = f"`{columns[0]}`" in documented
        else:
            found = all(column in documented for column in columns)
        if not found:
            unmentioned.append(tuple(columns))

    assert unmentioned == [], (
        f"database-schema.md describes {table_name} indexes as {documented!r} "
        f"but these exist and are unmentioned: {unmentioned}"
    )


def test_no_document_enumerates_a_stale_subset_of_the_languages() -> None:
    """
    Ukrainian was added, and three documents went on listing three languages.

    `README.md` described the locale directory as "en / ru / de", the schema
    still typed `users.language` as `ru` / `en` / `de`, and CLAUDE.md instructed
    future contributors to keep *three* catalogs in sync. Each was a run of
    language codes that simply never gained the fourth. Any such run — brace
    set, slash list, or comma list — must name every supported language.
    """
    codes = {member.value for member in LanguageCode}
    alternation = "|".join(sorted(codes))
    token = r"[`{(]?\b(?:" + alternation + r")\b[`)}]?"
    run = re.compile(token + r"(?:\s*[/,]\s*" + token + r")+")

    sources = dict(TEXT)
    guidance = ROOT / "CLAUDE.md"
    if guidance.exists():
        sources["CLAUDE.md"] = guidance.read_text(encoding="utf-8")

    stale: list[str] = []
    for name, body in sources.items():
        for line in body.splitlines():
            for match in run.finditer(line):
                listed = {c for c in codes if re.search(rf"\b{c}\b", match.group(0))}
                if len(listed) >= 2 and listed != codes:
                    stale.append(f"{name}: {line.strip()[:100]}")

    assert stale == [], "these lines enumerate languages but omit one:\n  " + "\n  ".join(stale)


def test_env_example_names_every_variable_compose_consumes() -> None:
    """
    A clean-room deploy is only as good as the file you are told to copy.

    `README.md` says `cp .env.example .env`, set three values, and start. But
    Compose also reads POSTGRES_PASSWORD, and `.env.example` did not mention it —
    so following the quick start deployed a database whose password was the
    default `vshop`, while deployment.md separately instructed the operator to
    "set a strong DB password" without naming the variable. Anything Compose
    reads has to be visible in the file the operator actually opens.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    consumed = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose))
    consumed -= {"DATABASE_URL"}  # set by compose itself, documented as overridden

    # A mention in prose is not a setting. Require a real assignment line, live
    # or commented out, so an operator can see the name and edit it in place.
    declared = {
        match.group(1) for match in re.finditer(r"(?m)^\s*#?\s*([A-Z_][A-Z0-9_]*)=", example)
    }
    missing = sorted(consumed - declared)
    assert missing == [], (
        f".env.example never declares these, but docker-compose.yml reads them: {missing}"
    )


def test_the_build_can_be_given_extra_trust_anchors() -> None:
    """
    The image build has to survive a TLS-intercepting network.

    `pip install` inside the build talks to PyPI from a container that trusts
    only public CAs. On a corporate proxy — or a consumer antivirus with HTTPS
    scanning — the certificate is re-signed by an authority the container has
    never seen, and the build dies at the first dependency while every other
    tool on the host works. The project already concedes such networks exist
    (`TELEGRAM_SSL_VERIFY`); the build needs the same escape hatch.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ca_dir = ROOT / "docker" / "ca-certificates"

    assert ca_dir.is_dir(), "docker/ca-certificates/ must ship, even empty"
    assert "docker/ca-certificates/" in dockerfile, (
        "the Dockerfile no longer copies operator-supplied trust anchors"
    )
    assert "update-ca-certificates" in dockerfile
    assert "--cert /etc/ssl/certs/ca-certificates.crt" in dockerfile, (
        "pip must be pointed at the system trust store, or an added CA has no effect"
    )
    assert "The build fails at `pip install` with a certificate error" in TEXT["deployment.md"], (
        "the failure and its fix must stay documented in the deployment runbook"
    )


def test_no_ca_certificate_is_committed_to_the_repository() -> None:
    """
    A CA belongs to one network, never to the repository.

    Committing one would make every build everywhere trust that authority —
    the opposite of what the escape hatch is for.

    The check is on what git *tracks*, not on what sits in the working tree.
    An operator behind a TLS-intercepting proxy is instructed to drop their own
    `.crt` into `docker/ca-certificates/`; an earlier version of this test
    scanned the filesystem and so failed for exactly the people who had followed
    the documented procedure.
    """
    ignore = (ROOT / "docker" / "ca-certificates" / ".gitignore").read_text(encoding="utf-8")
    assert "*.crt" in ignore, "the CA directory must ignore certificates"

    try:
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git available
        pytest.skip("git is not available to inspect tracked files")
    if tracked.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip("not a git checkout")

    certs = sorted(
        path for path in tracked.stdout.split() if path.endswith((".crt", ".pem", ".cer"))
    )
    assert certs == [], f"certificates must not be committed: {certs}"


def test_the_quick_start_matches_the_deployment_runbook() -> None:
    """Both entry points must name the same file and the same command."""
    readme = TEXT["README.md"]
    assert "cp .env.example .env" in readme
    assert "docker volume create vshop_pgdata" in readme
    assert "docker compose up" in readme
    assert "docker volume create vshop_pgdata" in TEXT["deployment.md"]
    assert "docker compose up -d --build" in TEXT["deployment.md"]


def test_the_volume_setup_is_in_the_deployment_runbook() -> None:
    """
    Not merely "documented somewhere".

    Compose never creates the database volume, so the runbook an operator
    follows must say how to create one for a new deployment and how to name the
    existing one when upgrading — otherwise they point the stack at storage that
    does not hold their data, or cannot start it at all.
    """
    deployment = TEXT["deployment.md"]
    assert "docker volume create vshop_pgdata" in deployment
    assert 'echo "POSTGRES_VOLUME_NAME=vshop_pgdata" >> .env' in deployment
    assert "POSTGRES_VOLUME_NAME=v-shop_pgdata" in deployment, "the upgrade example is gone"
