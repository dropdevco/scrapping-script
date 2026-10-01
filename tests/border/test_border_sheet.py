"""The crossing-times tab of the knowledge-base sheet.

GitHub Actions -> Supabase -> Google Sheet -> GoHighLevel: the bot answers from the
sheet, so a crossing time that never reaches it does not exist for the bot. The same
importer reads this tab as the engine's event export, so the same contract holds
(docs/components/knowledge-base-export.md): no empty cell, ever (GoHighLevel rejects
the row); a bilingual `content` sentence per row, because rows are what it embeds;
append-only headers; and never publishing an empty sheet.
"""

from __future__ import annotations

import contextlib
import io
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from scraper.border import sheet


def row(port="240202", lane="car", **over):
    base = {
        "port_number": port, "lane": lane,
        "bridge_es": "Paso del Norte (Santa Fe)", "bridge_en": "Paso del Norte",
        "also_known_as": "Centro, Santa Fe, PDN",
        "lane_es": "Autos", "lane_en": "Cars", "state": "open",
        "wait_es": "48 min", "wait_en": "48 min",
        "summary_es": "Paso del Norte (Santa Fe) · Autos: 48 min según CBP (revisado el 29 sep 2026, "
                      "2:10 p. m.). También le dicen Centro, Santa Fe o PDN.",
        "summary_en": "Paso del Norte · Cars: 48 min per CBP (checked Sep 29, 2026, 2:10 pm). "
                      "Also known as Centro, Santa Fe or PDN.",
        "minutes": 48, "cbp_minutes": 48, "source": "cbp", "lanes_open": 4,
        "cbp_updated_at": "2026-09-29T14:00:00-06:00",
        "checked_at": datetime.now(UTC).isoformat(),
    }
    return {**base, **over}


NOW = datetime(2026, 9, 29, 20, 10, tzinfo=UTC)


def test_the_header_is_the_append_only_contract():
    """Reordering these silently rewrites every field for an existing GHL import."""
    assert sheet.HEADERS[:15] == [
        "content", "bridge_es", "bridge_en", "also_known_as", "lane_es", "lane_en", "wait_es",
        "wait_en", "state", "minutes", "source", "checked_at", "data_current_as_of",
        "port_number", "lane_id"]


def test_no_cell_is_ever_empty():
    closed = row(lane="car_ready", state="closed", wait_es="cerrado", wait_en="closed",
                 minutes=None, cbp_minutes=None, lanes_open=None)
    values = sheet.build_values([row(), closed], NOW)
    assert all(cell.strip() for line in values for cell in line)
    assert values[2][sheet.HEADERS.index("minutes")] == "Not listed"


def test_content_is_one_bilingual_sentence_that_names_centro():
    content = sheet.build_values([row()], NOW)[1][0]
    english, spanish = content.split("\nES: ")
    assert english.startswith("Paso del Norte · Cars: 48 min")
    assert "También le dicen Centro" in spanish


def test_rows_follow_the_bridges_usual_order():
    values = sheet.build_values([row("240801"), row("240202", "walk"), row("240202", "car")], NOW)
    assert [(v[-2], v[-1]) for v in values[1:]] == [("240202", "car"), ("240202", "walk"), ("240801", "car")]


class Store:
    enabled = True

    def __init__(self, rows):
        self.rows = rows

    async def current_waits(self):
        return self.rows


async def test_it_is_off_until_a_tab_is_named():
    with mock.patch.object(sheet, "BorderStore", side_effect=AssertionError("must not connect")):
        assert await sheet.run(tab="") == 0


async def test_an_empty_table_is_never_published():
    with contextlib.redirect_stderr(io.StringIO()) as err:
        assert await sheet.run(tab="crossing_times", storage=Store([])) == 1
    assert "refusing to publish an empty sheet" in err.getvalue()


async def test_it_refuses_the_events_tab():
    err = io.StringIO()
    with mock.patch.object(sheet.settings, "kb_sheet_tab", "events"), contextlib.redirect_stderr(err):
        assert await sheet.run(tab="Events", storage=Store([row()])) == 1
    assert "KB_SHEET_TAB" in err.getvalue()


async def test_it_writes_the_named_tab_through_the_engines_writer():
    written = {}

    def write_sheet(values, *, tab):
        written.update(values=values, tab=tab)
        return len(values) - 1

    with mock.patch("scraper.kb.sheets.write_sheet", write_sheet), contextlib.redirect_stdout(io.StringIO()):
        assert await sheet.run(tab="crossing_times", storage=Store([row(), row(lane="walk")])) == 0
    assert written["tab"] == "crossing_times"
    assert len(written["values"]) == 3


async def test_a_stopped_poller_is_said_loudly_but_still_published(caplog):
    old = row(checked_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat())
    with mock.patch("scraper.kb.sheets.write_sheet", lambda values, *, tab: 1), \
         contextlib.redirect_stdout(io.StringIO()):
        assert await sheet.run(tab="t", storage=Store([old])) == 0
    assert "is border-poll still running" in caplog.text


@pytest.mark.parametrize("dry_run", [True])
async def test_dry_run_writes_nothing(dry_run):
    with mock.patch("scraper.kb.sheets.write_sheet", side_effect=AssertionError("must not write")), \
         contextlib.redirect_stdout(io.StringIO()) as out:
        assert await sheet.run(dry_run=dry_run, tab="t", storage=Store([row()])) == 0
    assert "[dry-run] 1 rows" in out.getvalue()
