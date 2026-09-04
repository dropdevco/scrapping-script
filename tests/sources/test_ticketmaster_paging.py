"""Ticketmaster pagination.

Results come back sorted date-ascending and `size` is capped at 200, so a
single-request fetch silently drops the FURTHEST-OUT events once a city has
more than 200 in the window — exactly the events a "save the date" post is
built from. Measured for El Paso on 2026-09-03: a 200-day window returned 187
on one page, a 240-day window 215 across two.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from scraper.core.config import settings
from scraper.core.models import Kind, SearchParams
from scraper.sources import events_ticketmaster as tm


class FakeHttp:
    """Serves `total` events, date-ascending, in pages of _PAGE_SIZE."""

    def __init__(self, total: int, page_size: int = tm._PAGE_SIZE):
        self.total = total
        self.page_size = page_size
        self.pages_requested: list[int] = []

    async def get_json(self, url, **kwargs):
        params = kwargs["params"]
        page = int(params.get("page", 0))
        self.pages_requested.append(page)
        start = page * self.page_size
        chunk = range(start, min(start + self.page_size, self.total))
        return {
            "_embedded": {
                "events": [
                    {
                        "name": f"Event {i}",
                        "id": str(i),
                        "url": "https://example.test",
                        "dates": {"start": {"localDate": "2027-03-05"}},
                        "_embedded": {"venues": [{"name": "A Venue"}]},
                    }
                    for i in chunk
                ]
            },
            "page": {
                "totalElements": self.total,
                "totalPages": -(-self.total // self.page_size),
                "number": page,
            },
        }


def _params(limit=400):
    today = date.today()
    return SearchParams(
        kind=Kind.EVENTS,
        location="El Paso",
        start_date=today,
        end_date=today + timedelta(days=240),
        limit=limit,
    )


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "ticketmaster_api_key", "test-key")


async def test_a_single_page_does_not_request_a_second():
    http = FakeHttp(total=187)
    events = await tm.SOURCE.fetch(_params(), http)
    assert len(events) == 187
    assert http.pages_requested == [0]


async def test_a_second_page_is_fetched_rather_than_silently_dropped():
    """The regression this file exists for: 215 events in two pages used to
    come back as the 200 nearest, losing the far end entirely."""
    http = FakeHttp(total=215)
    events = await tm.SOURCE.fetch(_params(), http)
    assert len(events) == 215
    assert http.pages_requested == [0, 1]


async def test_paging_stops_at_the_caller_s_limit():
    http = FakeHttp(total=1000)
    events = await tm.SOURCE.fetch(_params(limit=250), http)
    assert len(events) == 250


async def test_paging_is_bounded_even_when_the_api_claims_many_pages():
    """Ticketmaster refuses deep paging past ~1000 items anyway; the cap stops
    a pathological response turning one scrape into hundreds of requests."""
    http = FakeHttp(total=100_000)
    await tm.SOURCE.fetch(_params(limit=100_000), http)
    assert len(http.pages_requested) == tm._MAX_PAGES


async def test_an_empty_page_ends_paging():
    """Defends against a totalPages that overstates what the API will serve —
    without this the loop would keep asking for pages that return nothing."""
    http = FakeHttp(total=0)
    assert await tm.SOURCE.fetch(_params(), http) == []
    assert http.pages_requested == [0]
