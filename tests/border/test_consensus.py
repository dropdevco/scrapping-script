"""Reconciling sources: closures follow CBP, minutes follow freshness, mirrors never vote.

Three sites agreeing is one source agreeing with itself when two of them republish CBP
verbatim, so only independent sources count toward agreement.
"""

from __future__ import annotations

import unittest

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    setUpModule,
    tearDownModule,
)

from scraper.border import consensus
from scraper.border.sources.base import Reading


class Consensus(unittest.IsolatedAsyncioTestCase):
    def reading(self, source, minutes, age=5, state="open", independent=True, lane="car"):
        return Reading(port_number="240202", lane=lane, state=state, minutes=minutes,
                       age_minutes=age, source=source, independent=independent)

    async def test_one_source_is_reported_as_single(self):
        d = consensus.reconcile([self.reading("cbp", 40)])
        self.assertEqual((d.agreement, d.minutes, d.chosen_source), ("single", 40, "cbp"))

    async def test_close_numbers_agree_and_the_fresher_one_wins(self):
        d = consensus.reconcile([self.reading("cbp", 43, age=45),
                                 self.reading("pasosfronterizos", 48, age=8)])
        self.assertEqual(d.agreement, "agree")
        self.assertEqual((d.minutes, d.chosen_source), (48, "pasosfronterizos"))

    async def test_far_apart_numbers_are_a_conflict_with_the_spread_kept(self):
        d = consensus.reconcile([self.reading("cbp", 20, age=50),
                                 self.reading("pasosfronterizos", 60, age=5)])
        self.assertEqual(d.agreement, "conflict")
        self.assertEqual(d.spread_minutes, 40)
        self.assertTrue(d.disputed)

    async def test_long_waits_get_proportional_tolerance(self):
        d = consensus.reconcile([self.reading("cbp", 80, age=50),
                                 self.reading("pasosfronterizos", 95, age=5)])
        self.assertEqual(d.agreement, "agree")   # 15 apart on an 80-minute line

    async def test_a_closure_follows_cbp_even_when_a_site_shows_minutes(self):
        d = consensus.reconcile([self.reading("cbp", None, state="closed"),
                                 self.reading("pasosfronterizos", 35)])
        self.assertEqual((d.state, d.minutes, d.agreement), ("closed", None, "closed"))

    async def test_mirrors_do_not_count_as_corroboration(self):
        d = consensus.reconcile([self.reading("cbp", 43, age=45),
                                 self.reading("mirror", 43, age=45, independent=False)])
        self.assertEqual(d.agreement, "single")
        self.assertIsNone(d.spread_minutes)

    async def test_every_opinion_is_kept_for_audit(self):
        d = consensus.reconcile([self.reading("cbp", 43), self.reading("pasosfronterizos", 50)])
        self.assertEqual({o["source"] for o in d.opinions}, {"cbp", "pasosfronterizos"})
