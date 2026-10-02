"""The border-poll workflow and the CLI it calls.

The schedule lives in YAML, so that is where these reach — the same move as
tests/social/test_scheduling.py. Two things are easy to break without noticing: the
daily job is selected by comparing github.event.schedule against a cron string, so
editing the cron without the comparison silently stops retention and the live
selfcheck; and a workflow that reads a secret under the wrong name runs green while
storing nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scraper.border.__main__ import COMMANDS, main

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/border_poll.yml"


def _crons() -> list[str]:
    return re.findall(r'- cron: "([^"]+)"', WORKFLOW.read_text(encoding="utf-8"))


def test_the_daily_cron_is_the_one_the_jobs_test_for():
    text = WORKFLOW.read_text(encoding="utf-8")
    daily = [c for c in _crons() if not c.startswith("*/")]
    assert len(daily) == 1, f"expected exactly one daily cron, got {daily}"
    assert f"github.event.schedule == '{daily[0]}'" in text      # the selfcheck job
    assert f'[ "$SCHEDULE" = "{daily[0]}" ]' in text             # the --prune branch


def test_the_knowledge_base_is_refreshed_every_ten_minutes():
    """Agreed with Carlos, 2026-09-29: border_current_waits feeds the knowledge base and
    carries pasosfronterizos' fresher number, so it is rewritten every 10 minutes."""
    frequent = [c for c in _crons() if c.startswith("*/")]
    assert frequent == ["*/10 * * * *"]


def test_the_workflow_reads_the_engines_secret_names():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}" in text
    assert "SUPABASE_SERVICE_KEY" not in text


def test_every_workflow_command_exists():
    commands = re.findall(r"python -m scraper\.border (\w+)", WORKFLOW.read_text(encoding="utf-8"))
    assert commands and set(commands) <= set(COMMANDS)


def test_an_unknown_command_prints_usage_and_fails(capsys):
    assert main(["nonsense"]) == 2
    assert "python -m scraper.border poll" in capsys.readouterr().out


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_every_command_resolves_to_a_module_with_main(command):
    import importlib

    assert callable(importlib.import_module(COMMANDS[command]).main)


MIGRATION = Path(__file__).resolve().parents[2] / "supabase/migrations/0019_border_poll_dispatch.sql"


def test_the_dispatch_clock_targets_this_workflow_and_stays_off_without_a_token():
    """GitHub's own cron fired ~4 times in 17 hours, so pg_cron dispatches the workflow.
    The SQL has to name the right workflow file and jobs, never hand the token to
    anon/authenticated, and do nothing when the vault secret is missing."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "workflows/border_poll.yml/dispatches" in sql
    assert WORKFLOW.exists()
    assert "from public, anon, authenticated" in sql
    assert "github_dispatch_token" in sql and "return null" in sql
    # The jobs the workflow's `job` input accepts are the ones the function allows.
    options = re.search(r'options: \[([^\]]+)\]', WORKFLOW.read_text(encoding="utf-8")).group(1)
    for job in re.findall(r'"(\w+)"', options):
        assert f"'{job}'" in sql


def test_the_dispatch_crons_match_the_cadence_the_workflow_documents():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "'*/10 * * * *'" in sql and "dispatch_border_poll('poll')" in sql
    daily = [c for c in _crons() if not c.startswith("*/")]
    assert f"'{daily[0]}'" in sql and "dispatch_border_poll('daily')" in sql
