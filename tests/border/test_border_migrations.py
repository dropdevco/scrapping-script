"""Migration 0021 (the estimator's tables), read as text.

Nothing here applies the SQL, and CI has no database, so these pin what a reviewer would
otherwise check by eye and a later edit could quietly break: every new table is deny-all
like 0013-0017, the file stays additive and re-runnable (invariants §8), Signal.to_row
(SPEC §3) and border_signals agree column for column, and Q-14's 30 days covers every row
that holds a sender hash, a parsed message, a message's text or a position.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scraper.border.signals import FROM_PEOPLE, Signal

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase/migrations"
MIGRATION = MIGRATIONS / "0021_border_estimator.sql"
TABLES = ("border_signals", "border_estimates", "border_source_trust", "border_reports")

# The border_signals columns no writer sends: the database fills them itself.
FILLED_BY_DATABASE = {"id", "captured_at"}

# Statements that remove or rewrite what a live database already holds (invariants §8).
NOT_ADDITIVE = ("drop ", "delete ", "truncate ", "alter column", "rename ")


def _sql(path: Path | None = None) -> str:
    """The file (0021 unless named) lower-cased with comments removed, so prose can never
    pass an assertion."""
    return re.sub(r"--[^\n]*", "", (path or MIGRATION).read_text(encoding="utf-8")).lower()


def _statements(sql: str) -> str:
    """What the migration itself runs: function bodies ($$ ... $$) and quoted text removed.
    A retention function's delete runs later, when it is called (0014, 0015), and a
    comment's wording is not a statement."""
    return re.sub(r"\$\$.*?\$\$|'[^']*'", "", sql, flags=re.DOTALL)


def _not_additive(sql: str) -> list[str]:
    statements = _statements(sql)
    return [word.strip() for word in NOT_ADDITIVE if word in statements]


def _columns(table: str) -> dict[str, str]:
    """Column name to the rest of its definition, from `create table ... ( ... );`."""
    body = re.search(rf"create table if not exists {table} \((.*?)\n\);", _sql(), re.DOTALL).group(1)
    columns = {}
    for line in re.split(r",\s*\n", body):
        name, _, definition = line.strip().partition(" ")
        if name != "primary":
            columns[name] = " ".join(definition.split())
    return columns


def _prune() -> tuple[str, str]:
    """The retention function's signature and body."""
    found = re.search(r"create or replace function border_prune_estimator\((.*?)\)(.*?\$\$.*?\$\$);",
                      _sql(), re.DOTALL)
    assert found, "the estimator's retention is one function, like border_prune in 0015"
    return " ".join(found.group(1).split()), " ".join(found.group(2).split())


@pytest.mark.parametrize("table", TABLES)
def test_every_table_is_deny_all_like_the_other_border_tables(table):
    sql = _sql()
    assert f"create table if not exists {table} (" in sql
    assert re.search(rf"alter table {table}\s+enable row level security;", sql)


def test_no_policy_or_grant_opens_any_of_them():
    sql = _sql()
    assert "create policy" not in sql and "grant " not in sql


def test_the_file_is_additive_and_rerunnable():
    sql = _sql()
    assert _not_additive(sql) == []
    # The only alter table statements switch RLS on for this file's own tables.
    assert set(re.findall(r"alter table (\w+)", sql)) == set(TABLES)
    assert not re.search(r"create (unique )?(table|index) (?!if not exists)", sql)
    assert "create function" not in sql


@pytest.mark.parametrize("name", ["0014_border_retention.sql",
                                  "0015_border_alerts_crossings_typical.sql"])
def test_the_additivity_check_passes_the_house_retention_functions(name):
    """0014 and 0015 are additive and their prune functions delete when called. A check
    that read the function body as the migration's own statement rejected them both."""
    assert _not_additive(_sql(MIGRATIONS / name)) == []
    assert _not_additive("delete from border_readings;") == ["delete"]


def test_a_signal_row_fills_every_column_the_database_does_not():
    """Signal.to_row (SPEC §3) against the table: a key with no column fails the whole
    batch insert, and a column nobody writes is a field the contract lost."""
    row = Signal(port_number="240202", lane="car", direction="north", kind="published",
                 source="cbp", state="open", minutes=20, independent=True,
                 observed_at=datetime(2026, 9, 22, 3, 40, tzinfo=UTC)).to_row()
    columns = _columns("border_signals")
    assert set(row) <= set(columns), set(row) - set(columns)
    assert set(columns) - set(row) == FILLED_BY_DATABASE
    assert columns["content_hash"].endswith("unique")
    # Set from evidence, never assumed: a CBP mirror defaulting to independent would vote.
    assert "default" not in columns["independent"]


@pytest.mark.parametrize("table", ["border_signals", "border_estimates"])
def test_every_row_names_a_port_the_pipeline_knows(table):
    assert _columns(table)["port_number"] == "text not null references border_ports(port_number)"


def test_an_estimate_is_stored_hidden():
    columns = _columns("border_estimates")
    assert columns["shown"] == "boolean not null default false"
    # CBP's closure is followed and a moving measured signal only flags it (SPEC §4.4, Q-2).
    assert columns["closure_conflict"] == "boolean not null default false"
    assert columns["state"] == "text not null"
    assert {"port_number", "lane", "direction", "minutes", "low", "high", "confidence",
            "method", "inputs", "computed_at"} <= set(columns)


def test_trust_is_one_row_per_source_direction_and_day():
    assert {"n", "mae", "bias", "within_10", "prior", "trust"} <= set(_columns("border_source_trust"))
    assert "primary key (source, direction, day)" in _sql()


def test_reports_go_whole_after_30_days_not_just_their_text():
    """Q-14 keeps the sender hash, the parsed values and the text 30 days. Blanking the
    text and position kept the rest of the row, hash included, for good."""
    signature, body = _prune()
    assert "keep_reports_days int default 30" in signature
    assert "delete from border_reports where received_at < now() - make_interval(days => keep_reports_days);" in body
    assert "update " not in body
    assert {"reporter_hash", "raw_text", "lat", "lng", "parsed", "outcome"} <= set(_columns("border_reports"))


def test_the_signals_made_from_people_go_with_their_reports():
    """A crowd or position signal carries the sender hash and the parsed message, and a
    hash kept anywhere outlives the promise; signals.FROM_PEOPLE is that list of kinds."""
    _, body = _prune()
    signals = re.search(r"delete from border_signals where (.*?);", body).group(1)
    assert "captured_at < now() - make_interval(days => keep_days)" in signals
    people = re.search(r"kind in \(([^)]*)\) and captured_at < now\(\) - "
                       r"make_interval\(days => keep_reports_days\)", signals)
    assert people, signals
    assert set(re.findall(r"'(\w+)'", people.group(1))) == FROM_PEOPLE


def test_signals_and_estimates_do_not_grow_without_bound():
    """29 of the fixtures' 47 signals are new rows on every poll, and every poll writes an
    estimate per lane, so both need retention like border_readings."""
    signature, body = _prune()
    assert "keep_days int default 60" in signature
    assert "delete from border_estimates where computed_at < now() - make_interval(days => keep_days);" in body
    assert "returns jsonb" in body


def test_the_retention_runs_as_the_caller():
    """With RLS on and no policies, an anon call through the API deletes nothing. Run as
    its owner, the same call would empty every table it touches."""
    _, body = _prune()
    assert "security definer" not in body
