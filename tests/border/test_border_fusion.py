"""Fusion (SPEC.md §4): weights, the weighted median and range, confidence, the crowd
guards and the closure flag, as pure functions over Signals.

The adversarial cases are the ones SPEC and ADR-0013 name: one wild scraper, crowd
brigading, all-stale signals, southbound with no CBP, a closure conflict. Expected numbers
are worked by hand in the comments from SPEC's [A] priors (fresh: CBP 0.6, another site
0.4, measured 0.5, crowd 0.3 x 0.3 = 0.09, position 0.5 x 0.6 = 0.3), so a changed weight
shows up here as arithmetic rather than as a mystery.
"""

from __future__ import annotations

import json
import math
import random
import re
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from border_support import NOW

from scraper.border import fusion
from scraper.border.cbp import LOCAL_TZ
from scraper.border.fusion import fuse, fuse_all, weighted_percentile
from scraper.border.signals import FROM_PEOPLE, Signal, from_reading
from scraper.border.sources.base import Reading

MIGRATION = Path(__file__).resolve().parents[2] / "supabase/migrations/0021_border_estimator.sql"
CAM = "cam:pdn-north"


def sig(**changes) -> Signal:
    """A fresh, open CBP signal for Paso del Norte's car lane, changed where needed."""
    base = {"port_number": "240202", "lane": "car", "direction": "north", "kind": "published",
            "source": "cbp", "state": "open", "minutes": 20, "observed_at": NOW,
            "independent": True}
    merged = {**base, **changes}
    if merged["state"] != "open":
        merged["minutes"] = None
    return Signal(**merged)


def published(source: str, minutes: int | None, age: float = 0, **changes) -> Signal:
    return sig(source=source, minutes=minutes, observed_at=NOW - timedelta(minutes=age),
               **changes)


def measured(minutes: int | None, low: int | None = None, high: int | None = None,
             age: float = 0, source: str = CAM, **changes) -> Signal:
    return sig(kind="measured", source=source, minutes=minutes, low=low, high=high,
               observed_at=NOW - timedelta(minutes=age), **changes)


def person(reporter: str, minutes: int | None, age: float = 0, kind: str = "crowd",
           source: str = "tg_bot", **changes) -> Signal:
    return sig(kind=kind, source=source, minutes=minutes, reporter=reporter,
               observed_at=NOW - timedelta(minutes=age), **changes)


def outcomes(estimate) -> list[tuple[str, str]]:
    return [(v.signal.reporter or v.signal.source, v.outcome) for v in estimate.votes]


def shape(e) -> tuple:
    return (e.state, e.minutes, e.low, e.high, e.confidence, e.closure_conflict, e.crowd_only)


# -- the table: every adversarial case SPEC and the roadmap name -----------------------

WILD_SCRAPER = [published("cbp", 18), published("pasosfronterizos", 20),
                measured(22, 18, 26), published("wildsite", 58)]
# Twenty different people all say 120 while the two sites say about 20.
BRIGADE = ([published("cbp", 20), published("pasosfronterizos", 22)]
           + [person(f"sybil{n:02}", 120) for n in range(20)])

CASES = [
    # W = 0.6 + 0.4 + 0.5 + 0.4 = 1.9. Sorted 18 (.6), 20 (.4) passes half (.95) -> 20.
    # The wild site holds 0.4 / 1.9 = 21 % of the weight, more than the top 10 %, so it is
    # the range's high end: the disagreement shows in the range and costs "high".
    ("one wild scraper", WILD_SCRAPER, ("open", 20, 18, 58, "medium", False, False)),
    # Uncapped, the crowd would weigh 20 x 0.09 = 1.8 against 1.0 and set the median at
    # 120. Capped at 25 % of 1.0, W = 1.25, and 20 (.6), 22 (.4) passes half -> 22.
    # The cap guards the median, not the range: capped, people hold 0.25 / 1.25 = 20 % of
    # the weight, more than the 10 % tail, so their 120 is the top of the range and costs
    # "high". This pins today's arithmetic. Whether a capped crowd may move the range is
    # an open question for the owner (SPEC §4.3 against §4.4), not a guarantee.
    ("crowd brigading", BRIGADE, ("open", 22, 20, 120, "medium", False, False)),
    # 0.6 x 0.5^5 = 0.019 and 0.4 x 0.5^4 = 0.025, both inside six half-lives: the fresher
    # wins the median, and the total says how little either is worth.
    ("old but still live",
     [published("cbp", 40, age=150), published("pasosfronterizos", 45, age=120)],
     ("open", 45, 40, 45, "low", False, False)),
    # Ten and eight half-lives: neither says anything about the lane now.
    ("all stale", [published("cbp", 40, age=300), published("pasosfronterizos", 45, age=240)],
     ("no_data", None, None, None, None, False, False)),
    # A five-hour-old CBP number is no other live signal (§4.3), so the two people who
    # agree show alone, labelled, instead of being capped to 25 % of almost nothing.
    ("a stale official number leaves a lane to its people",
     [published("cbp", 40, age=300), person("ana", 90), person("beto", 90)],
     ("open", 90, 90, 90, "low", False, True)),
    ("... and one person alone", [published("cbp", 40, age=300), person("ana", 90)],
     ("open", 90, 90, 90, "low", False, True)),
    # No CBP southbound (Q-4): a camera alone weighs 0.5, enough for "medium".
    ("southbound, a camera and no CBP",
     [measured(25, 20, 35, source="cam:pdn-south", direction="south")],
     ("open", 25, 20, 35, "medium", False, False)),
    # Nothing but people: no corroboration needed (§4.3 exception), always "low". Two
    # equal weights, so the median is the higher claim.
    ("southbound, people only",
     [person("ana", 30, direction="south"), person("beto", 45, direction="south")],
     ("open", 45, 30, 45, "low", False, True)),
    # One person alone on such a lane still shows, labelled crowd-only.
    ("southbound, one person", [person("ana", 30, direction="south")],
     ("open", 30, 30, 30, "low", False, True)),
    # CBP closed, a camera sees traffic: both facts, no number, no winner (Q-2).
    ("closure conflict", [published("cbp", None, state="closed"), measured(15, 10, 20)],
     ("closed", None, None, None, None, True, False)),
    # Equal weights at 20 and 30: the higher, since a wait promised short costs more.
    ("an exact tie", [published("pasosfronterizos", 20), published("wildsite", 30)],
     ("open", 30, 20, 30, "medium", False, False)),
    ("a single fresh CBP reading", [published("cbp", 40)],
     ("open", 40, 40, 40, "medium", False, False)),
    # 0.6 x 0.5^1.5 = 0.21, and CBP's prior is below high trust.
    ("a single CBP reading 45 minutes old", [published("cbp", 40, age=45)],
     ("open", 40, 40, 40, "low", False, False)),
    # W = 1.5 over two kinds. Lows 25 (.5), 28, 29 -> 25; highs 28, 29, 40 (.5) -> 40.
    # The camera's own range sets both ends, and 15 minutes is still "high".
    ("signals with their own ranges",
     [published("cbp", 28), published("pasosfronterizos", 29), measured(30, 25, 40)],
     ("open", 29, 25, 40, "high", False, False)),
    ("only a mirror", [published("borderswaittime", 99, independent=False)],
     ("no_data", None, None, None, None, False, False)),
]


@pytest.mark.parametrize(("signals", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_adversarial_cases(signals, expected):
    estimate = fuse(signals, NOW)
    assert shape(estimate) == expected
    if estimate.minutes is not None:
        assert estimate.low <= estimate.minutes <= estimate.high


def test_one_wild_scraper_cannot_drag_the_answer_where_a_mean_would():
    estimate = fuse(WILD_SCRAPER, NOW)
    mean = sum(v.weight * v.signal.minutes for v in estimate.counted) / estimate.total_weight
    assert round(mean) == 28            # the alternative ADR-0013 rejected
    assert estimate.minutes == 20       # a value a real signal claimed


def test_brigading_people_weigh_a_quarter_of_everyone_else_together():
    estimate = fuse(BRIGADE, NOW)
    people = sum(v.weight for v in estimate.counted if v.signal.kind == "crowd")
    others = sum(v.weight for v in estimate.counted if v.signal.kind != "crowd")
    assert people == pytest.approx(fusion.CROWD_SHARE_CAP * others)
    assert all(v.outcome == "counted" for v in estimate.votes)   # they corroborate each other
    (cap,) = {v.cap for v in estimate.counted if v.signal.kind == "crowd"}
    assert cap == pytest.approx(0.25 * 1.0 / 1.8)


@pytest.mark.parametrize("people", [
    [person(f"p{n}", 120, kind="position", source="tg_position") for n in range(6)],
    [person(f"p{n}", 120, kind="position", source="tg_position") for n in range(3)]
    + [person(f"c{n}", 120) for n in range(3)],
], ids=["position reports", "position and crowd together"])
def test_position_reports_share_the_crowd_cap(people):
    # §4.1: a position report uses the crowd guards. Uncapped, six of them at 0.3 would
    # weigh 1.8 against the sites' 1.0 and set the median at 120.
    estimate = fuse([published("cbp", 20), published("pasosfronterizos", 22), *people], NOW)
    weight = sum(v.weight for v in estimate.counted if v.signal.kind in FROM_PEOPLE)
    assert weight == pytest.approx(fusion.CROWD_SHARE_CAP * 1.0)
    assert estimate.minutes == 22


def test_the_cap_leaves_a_crowd_within_its_share_alone():
    # 0.6 + 0.4 leaves room for 0.25; two people at 0.09 each are 0.18.
    estimate = fuse([published("cbp", 20), published("pasosfronterizos", 22),
                     person("ana", 25), person("beto", 26)], NOW)
    assert [v.outcome for v in estimate.votes] == ["counted"] * 4
    assert {v.cap for v in estimate.votes} == {1.0}


# -- weights (§4.1, §4.2) ------------------------------------------------------------

WEIGHTS = [
    ("CBP, fresh", published("cbp", 30), None, 0.6),
    ("CBP, one half-life old", published("cbp", 30, age=30), None, 0.3),
    ("another site, fresh", published("pasosfronterizos", 30), None, 0.4),
    ("a camera, one half-life old", measured(30, 25, 35, age=10), None, 0.25),
    ("a person, one half-life old", person("ana", 30, age=20), None, 0.3 * 0.5 * 0.3),
    ("a position report, fresh", person("ana", 30, kind="position", source="tg_position"),
     None, 0.5 * 0.6),
    ("earned trust replaces the prior", published("cbp", 30), {("cbp", "north"): 0.9}, 0.9),
    ("trust is per direction", published("cbp", 30), {("cbp", "south"): 0.9}, 0.6),
    ("a stamp ahead of the clock is new, not fresher", published("cbp", 30, age=-5), None, 0.6),
]


@pytest.mark.parametrize(("signal", "trust", "weight"), [w[1:] for w in WEIGHTS],
                         ids=[w[0] for w in WEIGHTS])
def test_weight_is_trust_times_freshness_times_kind_factor(signal, trust, weight):
    (vote,) = fuse([signal], NOW, trust).votes
    assert vote.weight == pytest.approx(weight)
    assert vote.weight == pytest.approx(vote.trust * vote.freshness * vote.kind_factor)


def test_a_claim_so_old_its_weight_is_nothing_gives_no_number():
    estimate = fuse([published("cbp", 30, age=100_000)], NOW)   # stale long before 0.5^3333
    assert shape(estimate) == ("no_data", None, None, None, None, False, False)


@pytest.mark.parametrize(("signal", "limit"), [
    (published("cbp", 30), 180), (measured(30, 25, 35), 60), (person("ana", 30), 120),
    (person("ana", 30, kind="position", source="tg_position"), 60),
])
def test_a_claim_is_live_for_six_half_lives_of_its_kind(signal, limit):
    def outcome(age):
        aged = replace(signal, observed_at=NOW - timedelta(minutes=age))
        (vote,) = fuse([aged], NOW).votes        # alone, so a person needs no corroboration
        return vote.outcome
    assert (outcome(limit), outcome(limit + 1)) == ("counted", "stale")


def test_signals_older_than_history_change_nothing():
    lane = [published("cbp", 30, age=20), published("pasosfronterizos", 34), measured(31, 27, 36),
            person("ana", 33), person("beto", 35)]
    old = fusion.HISTORY + timedelta(minutes=1)
    ancient = [sig(state="closed", observed_at=NOW - old),
               published("pasosfronterizos", 90, age=old.total_seconds() / 60),
               person("caro", 90, age=old.total_seconds() / 60)]
    assert shape(fuse(lane + ancient, NOW)) == shape(fuse(lane, NOW))
    assert fuse(lane + ancient, NOW).to_row()["inputs"] == fuse(lane, NOW).to_row()["inputs"]


@pytest.mark.parametrize("bad", [0, -0.1, 1.5, math.nan, math.inf, True, "0.5"])
def test_a_trust_outside_zero_to_one_is_refused(bad):
    with pytest.raises(ValueError, match="trust"):
        fuse([published("cbp", 30)], NOW, {("cbp", "north"): bad})


# -- one vote per source ---------------------------------------------------------------

def test_a_source_votes_once_with_its_newest_claim():
    estimate = fuse([published("cbp", 30, age=60), published("cbp", 50)], NOW)
    assert estimate.minutes == 50
    assert [v.outcome for v in estimate.votes] == ["superseded", "counted"]


def test_a_site_that_refreshes_often_does_not_outvote_cbp_by_refreshing():
    # pasosfronterizos every 8 minutes for the past hour, all at 40, against one CBP at
    # 20. Counted row by row they would weigh about 1.7 against 0.6 and win.
    pasos = [published("pasosfronterizos", 40, age=age) for age in range(0, 56, 8)]
    estimate = fuse([published("cbp", 20), *pasos], NOW)
    assert estimate.minutes == 20
    assert [v.outcome for v in estimate.votes].count("counted") == 2


# CBP's 10:00 pm update. signals.py dates a closure when it is read (CBP gives a closed
# lane no update_time) and a number by its stamp, so the two clocks disagree.
SLOT = datetime(2026, 9, 22, 22, 0, tzinfo=LOCAL_TZ)


def cbp_read(minutes: int | None, read: datetime) -> tuple[Signal, datetime]:
    reading = Reading(port_number="240202", lane="car", source="cbp", minutes=minutes,
                      state="open" if minutes is not None else "closed",
                      label="At 10:00 pm MDT" if minutes is not None else None)
    return from_reading(reading, read), read


@pytest.mark.parametrize(("first", "then", "expected"), [
    # Read at 10:03, the page still says closed; at 10:13 the 10:00 update reopens it.
    ((None, 3), (25, 13), ("open", 25)),
    # The other way round: open at 10:13, closed when read at 10:23.
    ((25, 13), (None, 23), ("closed", None)),
])
def test_a_sources_newest_claim_is_the_one_read_last(first, then, expected):
    claims = [cbp_read(minutes, SLOT + timedelta(minutes=after)) for minutes, after in (first, then)]
    read_at = {signal.content_hash: read for signal, read in claims}
    signals = [signal for signal, _ in claims]
    for later in (23, 30, 59):           # the whole hour until CBP's next stamp
        estimate = fuse(signals, SLOT + timedelta(minutes=later), read_at=read_at)
        assert (estimate.state, estimate.minutes) == expected


def test_the_reopening_is_stamped_before_the_closure_it_replaces():
    # Why read_at matters: by observed_at alone the 10:03 closure is the newer claim.
    (closed, _), (reopened, _) = cbp_read(None, SLOT + timedelta(minutes=3)), \
        cbp_read(25, SLOT + timedelta(minutes=13))
    assert reopened.observed_at == SLOT < closed.observed_at


def test_a_naive_read_time_is_refused():
    signal = published("cbp", 30)
    with pytest.raises(ValueError, match="read_at"):
        fuse([signal, published("cbp", 35, age=5)], NOW,
             read_at={signal.content_hash: NOW.replace(tzinfo=None)})


def test_a_mirror_never_votes():
    estimate = fuse([published("cbp", 20), published("borderswaittime", 99, independent=False)],
                    NOW)
    assert (estimate.minutes, estimate.high) == (20, 20)
    assert sorted(outcomes(estimate)) == [("borderswaittime", "mirror"), ("cbp", "counted")]


# -- closures (§4.4, Q-2) --------------------------------------------------------------

CLOSED = published("cbp", None, state="closed")


def closed_for(minutes: int) -> list[Signal]:
    """CBP's closure as the poller stores it: read again every 10 minutes, each read a
    new claim dated when it was read."""
    return [published("cbp", None, age=age, state="closed") for age in range(minutes, -1, -10)]


CLOSURES = [
    ("a site showing minutes is a stale page, not a conflict",
     [CLOSED, published("pasosfronterizos", 35)], ("closed", False),
     [("cbp", "counted"), ("pasosfronterizos", "lane_closed")]),
    ("a camera that saw traffic after CBP first said closed is a conflict",
     [*closed_for(20), measured(15, 10, 20, age=10)], ("closed", True),
     [(CAM, "conflict"), ("cbp", "counted")] + [("cbp", "superseded")] * 2),
    ("a camera's count from before CBP first said closed is not",
     [*closed_for(10), measured(15, 10, 20, age=11)], ("closed", False),
     [(CAM, "lane_closed"), ("cbp", "counted"), ("cbp", "superseded")]),
    # ADR-0013 names Maps too. On the free tier it reads about every 52 minutes (§5.1).
    ("Maps read 40 minutes into an hour's closure is a conflict",
     [*closed_for(60), measured(15, 10, 20, age=40, source="maps")], ("closed", True),
     [("maps", "conflict"), ("cbp", "counted")] + [("cbp", "superseded")] * 6),
    ("Maps read before the closure began is not",
     [published("cbp", 25, age=50), *closed_for(30), measured(15, 10, 20, age=40, source="maps")],
     ("closed", False),
     [("maps", "lane_closed"), ("cbp", "counted")] + [("cbp", "superseded")] * 4),
    ("a camera dark for longer than it stays live is not",
     [*closed_for(90), measured(15, 10, 20, age=61)], ("closed", False),
     [(CAM, "stale"), ("cbp", "counted")] + [("cbp", "superseded")] * 9),
    # CBP's closure from 10 minutes ago still stands: Update Pending says nothing.
    ("Update Pending does not lift CBP's closure",
     [published("cbp", None, age=10, state="closed"), published("cbp", None, state="no_data"),
      published("pasosfronterizos", 35, age=3)], ("closed", False),
     [("cbp", "counted"), ("cbp", "no_minutes"), ("pasosfronterizos", "lane_closed")]),
    ("nor drop CBP's last number",
     [published("cbp", 25, age=5), published("cbp", None, state="no_data"),
      published("pasosfronterizos", 60, age=3)], ("open", False),
     [("cbp", "counted"), ("cbp", "no_minutes"), ("pasosfronterizos", "counted")]),
    ("a closure that has gone stale closes nothing",
     [published("cbp", None, age=181, state="closed"), published("pasosfronterizos", 35)],
     ("open", False), [("cbp", "stale"), ("pasosfronterizos", "counted")]),
    ("a camera that also sees it closed agrees",
     [CLOSED, measured(None, state="closed")], ("closed", False),
     [("cbp", "counted"), (CAM, "no_minutes")]),
    ("people never open a lane CBP closed",
     [CLOSED, person("ana", 30), person("beto", 31)], ("closed", False),
     [("cbp", "counted"), ("ana", "lane_closed"), ("beto", "lane_closed")]),
    ("CBP's newer opening replaces its closure",
     [published("cbp", None, age=40, state="closed"), published("cbp", 25)], ("open", False),
     [("cbp", "superseded"), ("cbp", "counted")]),
    ("Update Pending is no claim, so the others decide",
     [published("cbp", None, state="no_data"), published("pasosfronterizos", 35)],
     ("open", False), [("cbp", "no_minutes"), ("pasosfronterizos", "counted")]),
]


@pytest.mark.parametrize(("signals", "expected", "votes"), [c[1:] for c in CLOSURES],
                         ids=[c[0] for c in CLOSURES])
def test_closures_follow_cbp_and_a_conflict_is_flagged_not_resolved(signals, expected, votes):
    estimate = fuse(signals, NOW)
    assert (estimate.state, estimate.closure_conflict) == expected
    assert sorted(outcomes(estimate)) == sorted(votes)
    if estimate.state == "closed":
        assert (estimate.minutes, estimate.low, estimate.high, estimate.confidence) == (None,) * 4


def test_people_never_close_a_lane_either():
    open_lane = fuse([published("cbp", 30)]
                     + [person(name, None, state="closed") for name in ("ana", "beto", "caro")],
                     NOW)
    assert (open_lane.state, open_lane.minutes) == ("open", 30)
    no_one_else = fuse([person(name, None, state="closed") for name in ("ana", "beto")], NOW)
    assert no_one_else.state == "no_data"


# -- crowd guards (§4.3) ---------------------------------------------------------------

CORROBORATION = [
    ("a lone report", [person("ana", 35)], {"ana": "uncorroborated"}),
    ("two people close in time and minutes", [person("ana", 35, age=5), person("beto", 38)],
     {"ana": "counted", "beto": "counted"}),
    ("more than 20 minutes apart", [person("ana", 35, age=25), person("beto", 35)],
     {"ana": "uncorroborated", "beto": "uncorroborated"}),
    ("15 apart on a 35-minute line, past its 10", [person("ana", 35), person("beto", 50)],
     {"ana": "uncorroborated", "beto": "uncorroborated"}),
    ("18 apart on an 80-minute line, within its 20", [person("ana", 80), person("beto", 98)],
     {"ana": "counted", "beto": "counted"}),
    ("a position report corroborates a crowd report",
     [person("ana", 35), person("beto", 33, kind="position", source="tg_position")],
     {"ana": "counted", "beto": "counted"}),
    ("an implausible report corroborates nothing", [person("ana", 40), person("beto", 400)],
     {"ana": "uncorroborated", "beto": "implausible"}),
    # Fishing: beto's second number would match ana's, but his first holds his slot.
    ("a rate-limited report corroborates nothing",
     [person("ana", 40, age=10), person("beto", 90, age=9), person("beto", 41)],
     {"ana": "uncorroborated", "beto": "uncorroborated"}),
]


@pytest.mark.parametrize(("people", "expected"), [c[1:] for c in CORROBORATION],
                         ids=[c[0] for c in CORROBORATION])
def test_a_report_counts_only_when_a_second_person_agrees(people, expected):
    estimate = fuse([published("cbp", 30, age=5), *people], NOW)
    got = {v.signal.reporter: v.outcome for v in estimate.votes
           if v.signal.reporter and v.outcome != "rate_limited"}
    assert got == expected


def test_one_person_twice_is_not_two_people(monkeypatch):
    # At SPEC's numbers the 30-minute rate limit already keeps one person's reports
    # further apart than the 20-minute window. Tuned the other way, the rule still holds.
    monkeypatch.setattr(fusion, "RATE_LIMIT", timedelta(0))
    estimate = fuse([published("cbp", 30, age=5), person("ana", 35, age=5), person("ana", 36)],
                    NOW)
    assert [v.outcome for v in estimate.votes if v.signal.reporter] == ["uncorroborated"] * 2


def test_one_person_spamming_counts_once():
    spam = [person("ana", 120, age=age) for age in (25, 20, 15, 10, 5)]
    estimate = fuse([published("cbp", 20), *spam, person("beto", 120, age=24)], NOW)
    assert outcomes(estimate) == [("ana", "counted"), ("beto", "counted")] + \
        [("ana", "rate_limited")] * 4 + [("cbp", "counted")]


def test_a_person_counts_again_after_30_minutes():
    estimate = fuse([person("ana", 40, age=age) for age in (40, 30, 5)], NOW)   # crowd-only
    assert [v.outcome for v in estimate.votes] == ["counted", "rate_limited", "counted"]


@pytest.mark.parametrize(("minutes", "low", "high", "outcome"), [
    (0, None, None, "implausible"), (361, None, None, "implausible"),
    (40, 0, None, "implausible"), (1, None, None, "counted"), (360, None, None, "counted"),
])
def test_a_persons_numbers_must_be_a_believable_crossing(minutes, low, high, outcome):
    (vote,) = fuse([person("ana", minutes, low=low, high=high)], NOW).votes   # crowd-only
    assert vote.outcome == outcome


def test_a_published_number_is_what_was_said_however_large():
    assert fuse([published("pasosfronterizos", 400)], NOW).minutes == 400


def test_a_lower_bound_adds_nothing_above_its_own_claim():
    # "llevo 40 min" (SPEC §5.3): still in line, so at least 40, with no top.
    estimate = fuse([person("ana", 40, low=40)], NOW)
    assert (estimate.minutes, estimate.low, estimate.high) == (40, 40, 40)


def test_a_range_with_no_claim_does_not_vote():
    estimate = fuse([published("cbp", 30), measured(None, 20, 40)], NOW)
    assert (estimate.minutes, estimate.low, estimate.high) == (30, 30, 30)
    assert sorted(outcomes(estimate)) == [(CAM, "no_minutes"), ("cbp", "counted")]


# -- confidence (§4.5) -----------------------------------------------------------------

CONFIDENCE = [
    # 0.7 x 0.5^0.8 = 0.40: light, but earned trust above 2/3 and within its half-life.
    ("a single high-trust fresh signal", [measured(30, 25, 35, age=8)],
     {(CAM, "north"): 0.7}, "medium"),
    ("the same signal at its prior", [measured(30, 25, 35, age=8)], None, "low"),
    ("high trust but past its half-life", [measured(30, 25, 35, age=11)],
     {(CAM, "north"): 0.7}, "low"),
    # 0.3 + 0.25 = 0.55: two kinds and tight, but not heavy enough for "high".
    ("two kinds, tight, light", [published("cbp", 30, age=30), measured(31, 28, 34, age=10)],
     None, "medium"),
    # 0.6 + 0.4 = 1.0 and tight, but one kind.
    ("heavy and tight, one kind", [published("cbp", 30), published("pasosfronterizos", 31)],
     None, "medium"),
    # 1.1 over two kinds, 16 wide.
    ("heavy, two kinds, too wide", [published("cbp", 30), measured(31, 24, 40)], None, "medium"),
    ("heavy, two kinds, tight", [published("cbp", 30), measured(31, 25, 40)], None, "high"),
    ("crowd-only, however many agree",
     [person(f"p{n}", 30) for n in range(30)], None, "low"),
    # A camera dark for two hours is stale, so it is not a second kind.
    ("a dead camera is not a second kind",
     [published("cbp", 30), published("pasosfronterizos", 31), measured(30, 28, 33, age=120)],
     None, "medium"),
    # The channel earned 0.8, but the cap scales its people to 25 % of CBP's 0.019, and
    # the estimate is CBP's 2.5-hour-old number they contradict: not "medium".
    ("a capped person is not the single high-trust signal",
     [published("cbp", 40, age=150), person("ana", 90), person("beto", 90)],
     {("tg_bot", "north"): 0.8}, "low"),
]


@pytest.mark.parametrize(("signals", "trust", "word"), [c[1:] for c in CONFIDENCE],
                         ids=[c[0] for c in CONFIDENCE])
def test_confidence(signals, trust, word):
    assert fuse(signals, NOW, trust).confidence == word


# -- the median and percentiles --------------------------------------------------------

PERCENTILES = [
    ("an even split takes the higher at the median", [(20, 1.0), (30, 1.0)], 0.5, 30),
    ("and the lower at the 10th", [(20, 1.0), (30, 1.0)], 0.1, 20),
    ("and the higher at the 90th", [(20, 1.0), (30, 1.0)], 0.9, 30),
    ("a heavier middle holds", [(10, 1.0), (20, 2.0), (30, 1.0)], 0.5, 20),
    ("order does not matter", [(30, 1.0), (10, 1.0), (20, 2.0)], 0.5, 20),
    ("a weightless claim is never chosen", [(10, 0.0), (20, 1.0)], 0.1, 20),
    ("repeated values pool their weight", [(10, 1.0), (40, 0.6), (40, 0.6)], 0.5, 40),
]


@pytest.mark.parametrize(("claims", "share", "value"), [p[1:] for p in PERCENTILES],
                         ids=[p[0] for p in PERCENTILES])
def test_weighted_percentile(claims, share, value):
    assert weighted_percentile(claims, share) == value


@pytest.mark.parametrize("claims", [[], [(10, 0.0)]])
def test_a_percentile_of_nothing_is_refused(claims):
    with pytest.raises(ValueError):
        weighted_percentile(claims, 0.5)


# -- determinism and input checks ------------------------------------------------------

def test_the_same_signals_in_any_order_give_the_same_estimate():
    # Superseding, corroboration, the cap and the median all in play at once.
    signals = (WILD_SCRAPER + BRIGADE[2:6]
               + [published("cbp", 25, age=40), person("ana", 60, age=3), person("ana", 61)])
    first = fuse(signals, NOW)
    for seed in range(25):
        assert fuse(random.Random(seed).sample(signals, len(signals)), NOW) == first


def test_two_claims_from_one_source_at_one_moment_settle_the_same_either_way():
    a, b = published("pasosfronterizos", 20), published("pasosfronterizos", 35)
    assert fuse([a, b], NOW).minutes == fuse([b, a], NOW).minutes


@pytest.mark.parametrize(("signals", "now"), [
    ([], NOW),
    ([published("cbp", 20), published("cbp", 20, lane="car_sentri")], NOW),
    ([published("cbp", 20), published("cbp", 20, port_number="250401")], NOW),
    ([measured(20), measured(20, direction="south")], NOW),
    ([published("cbp", 20)], NOW.replace(tzinfo=None)),
])
def test_fuse_refuses_what_is_not_one_lane_at_one_moment(signals, now):
    with pytest.raises(ValueError):
        fuse(signals, now)


def test_fuse_all_keeps_lanes_and_directions_apart():
    signals = [published("cbp", 20), published("cbp", 5, lane="car_sentri"),
               measured(40, 35, 45, source="cam:pdn-south", direction="south")]
    trust = {("cam:pdn-south", "north"): 0.9}        # northbound trust is not southbound's
    estimates = fuse_all(signals, NOW, trust)
    assert {key: e.minutes for key, e in estimates.items()} == {
        ("240202", "car", "north"): 20, ("240202", "car_sentri", "north"): 5,
        ("240202", "car", "south"): 40}
    (south,) = estimates[("240202", "car", "south")].votes
    assert south.trust == fusion.PRIOR["measured"]


# -- the stored row (SPEC §6, 0021) ----------------------------------------------------

def _estimate_columns() -> set[str]:
    sql = re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))
    body = re.search(r"create table if not exists border_estimates \((.*?)\n\);", sql,
                     re.DOTALL).group(1)
    return {line.split()[0] for line in body.strip().splitlines() if line.strip()}


def test_a_row_fills_every_estimate_column_but_the_ones_the_database_owns():
    row = fuse(BRIGADE, NOW).to_row()
    assert set(row) == _estimate_columns() - {"id", "shown"}
    assert row["method"] == "fusion_v1"
    assert datetime.fromisoformat(row["computed_at"]) == NOW
    json.dumps(row)                       # jsonb-ready as it stands


def test_a_row_cites_each_input_and_never_who_sent_it():
    estimate = fuse(BRIGADE, NOW)
    row = estimate.to_row()
    assert "sybil" not in json.dumps(row)                  # Q-14: no reporter hash
    assert len(row["inputs"]) == len(estimate.counted)
    for vote, entry in zip(estimate.counted, row["inputs"], strict=True):
        people = vote.signal.kind in FROM_PEOPLE
        assert entry["signal"] == (None if people else vote.signal.content_hash)
        assert entry["weight"] == pytest.approx(
            entry["trust"] * entry["freshness"] * entry["kind_factor"] * entry["cap"])
        assert "evidence" not in entry and "reporter_hash" not in entry


def test_a_row_is_the_same_whoever_sent_the_reports():
    # A cited content_hash is a sha1 over the reporter_hash: with a candidate sender's
    # hash, it would confirm who reported what long after Q-14's 30 days.
    def inputs(*senders):
        row = fuse([published("cbp", 20), person(senders[0], 22), person(senders[1], 23)],
                   NOW).to_row()
        return sorted(json.dumps(entry, sort_keys=True) for entry in row["inputs"])
    assert inputs("ana", "beto") == inputs("rh_7c9e", "rh_0000")


def test_a_conflict_row_names_the_signal_that_saw_traffic():
    row = fuse([CLOSED, measured(15, 10, 20), published("pasosfronterizos", 30)], NOW).to_row()
    assert row["closure_conflict"] is True
    assert sorted((i["source"], i["outcome"]) for i in row["inputs"]) == [
        (CAM, "conflict"), ("cbp", "counted")]
