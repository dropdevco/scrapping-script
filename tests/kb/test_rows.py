"""Row-building rules for the GHL knowledge-base export."""

from __future__ import annotations

import pytest

from scraper.kb import rows as rows_mod

BASE = {
    "id": "abc-123",
    "title": "Bad Bunny",
    "start_time": "2026-09-12T02:00:00+00:00",  # 8pm Sep 11 local (America/Denver)
    "venue": "Don Haskins Center",
    "location": "201 Glory Rd, El Paso, TX",
    "categories": ["Music"],
    "ticket_links": [{"source": "events_ticketmaster", "label": "Ticketmaster", "url": "https://tm.test/bb"}],
    "url": "https://example.test/fallback",
}


def build(**overrides):
    return rows_mod.build_row(
        {**BASE, **overrides}, site_base_url="https://epchisme.com", generated_on="2026-08-27"
    )


def cell(row, name):
    return row[rows_mod.HEADERS.index(name)]


def test_content_is_prose_in_both_languages():
    content = cell(build(), "content")
    en, es = content.split("\nES: ")
    assert "Bad Bunny — Friday, September 11, 2026 at 8:00 PM" in en
    assert "Don Haskins Center, 201 Glory Rd, El Paso, TX" in en
    assert "viernes, 11 de septiembre de 2026 a las 8:00 PM" in es
    assert "Boletos: https://tm.test/bb" in es


def test_content_carries_no_relative_dates():
    content = cell(build(), "content").lower()
    for phrase in ("tonight", "this weekend", "tomorrow", "days from now", "hoy", "mañana"):
        assert phrase not in content


def test_ticket_link_wins_over_source_url():
    assert cell(build(), "tickets_url") == "https://tm.test/bb"
    assert cell(build(ticket_links=[]), "tickets_url") == "https://example.test/fallback"


def test_venue_join_overrides_raw_columns():
    row = build(venues={"name": "Don Haskins Center (UTEP)", "address": "151 Glory Road, El Paso, TX 79968"})
    assert cell(row, "venue") == "Don Haskins Center (UTEP)"
    assert cell(row, "address") == "151 Glory Road, El Paso, TX 79968"


def test_implausible_time_is_omitted_not_guessed():
    # 03:00 local — a parse we can't vouch for, so no time reaches the customer.
    row = build(start_time="2026-09-12T09:00:00+00:00")
    assert cell(row, "time") == rows_mod.NOT_LISTED
    assert "at 3:00 AM" not in cell(row, "content")
    assert "September 12, 2026" in cell(row, "content")


def test_more_info_url_points_at_the_event_page():
    assert cell(build(), "more_info_url") == "https://epchisme.com/events/abc-123"


@pytest.mark.parametrize("title", ["Events", "upcoming events", "Calendar", "", None])
def test_boilerplate_titles_are_dropped(title):
    assert build(title=title) is None


def test_rows_without_a_start_time_are_dropped():
    assert build(start_time=None) is None


def test_build_sheet_emits_a_header_and_only_valid_rows():
    values = rows_mod.build_sheet(
        [BASE, {**BASE, "title": "Events"}, {**BASE, "id": "d-2"}],
        site_base_url="https://epchisme.com",
        generated_on="2026-08-27",
    )
    assert values[0] == rows_mod.HEADERS
    assert len(values) == 3  # header + 2 real events
    assert all(len(r) == len(rows_mod.HEADERS) for r in values)


def test_no_cell_is_ever_empty():
    """GHL's importer rejects a row with any blank cell, which fails the whole
    import rather than one field. Strip the row down to nothing optional."""
    row = build(
        venue=None, location=None, categories=[], ticket_links=[], url=None,
        description=None, start_time="2026-09-12T09:00:00+00:00",
    )
    assert all(c.strip() for c in row), row
    assert cell(row, "venue") == rows_mod.NOT_LISTED
    assert cell(row, "tickets_url") == rows_mod.NOT_LISTED


def test_placeholder_never_lands_in_the_prose_cell():
    row = build(venue=None, location=None, categories=[], ticket_links=[], url=None)
    assert rows_mod.NOT_LISTED not in cell(row, "content")
