"""Metrics collection: credential hygiene and the degrade-and-retry loop.

The redaction tests are not incidental. Every Graph call carries its token in
the query string, httpx puts the failing URL in the exception it raises, and
this pipeline writes exception text into `ig_posts.error` AND broadcasts it to
Telegram and email — so a missing redaction publishes a live credential to
three places at once.
"""

from __future__ import annotations

import pytest

from scraper.core.http import redact_secrets
from scraper.social import metrics


# ── redaction ─────────────────────────────────────────────────────────────────
def test_access_token_in_a_graph_url_is_redacted():
    text = "400 from https://graph.instagram.com/v21.0/1/insights?metric=reach&access_token=IGAAb5ts_x-Y"
    out = redact_secrets(text)
    assert "IGAAb5ts_x-Y" not in out
    assert "access_token=***" in out


def test_redaction_keeps_the_error_body_that_follows_the_token():
    """A greedy value class would eat the colon and Meta's explanation with
    it, redacting away the whole reason the message exists."""
    text = (
        "400 from https://graph.instagram.com/v21.0/1/insights?access_token=ABC123: "
        '{"error": {"message": "does not support the impressions metric"}}'
    )
    out = redact_secrets(text)
    assert "ABC123" not in out
    assert "does not support the impressions metric" in out


@pytest.mark.parametrize("param", ["access_token", "secret_token", "api_key", "apikey"])
def test_every_credential_param_is_covered(param):
    assert "sekrit" not in redact_secrets(f"https://x.test/y?{param}=sekrit&other=1")


def test_non_secret_query_params_survive():
    out = redact_secrets("https://x.test/y?metric=reach&period=day&access_token=ABC")
    assert "metric=reach" in out and "period=day" in out


def test_plain_text_is_untouched():
    assert redact_secrets("only 3/6 slides were accepted") == "only 3/6 slides were accepted"


# ── degrade and retry ─────────────────────────────────────────────────────────
class FakeHttp:
    """Rejects any metric named in `unsupported`, the way Meta does."""

    def __init__(self, unsupported=(), values=None):
        self.unsupported = set(unsupported)
        self.values = values or {"reach": 12}
        self.attempts: list[list[str]] = []

    async def get_json(self, url, **kwargs):
        asked = kwargs["params"]["metric"].split(",")
        self.attempts.append(asked)
        for name in asked:
            if name in self.unsupported:
                raise RuntimeError(
                    f"400 from {url}?access_token=***: "
                    f'{{"error": {{"message": "The Media Insights API does not support '
                    f'the {name} metric for this media product type."}}}}'
                )
        return {
            "data": [
                {"name": n, "values": [{"value": self.values.get(n, 0)}]} for n in asked
            ]
        }


async def test_all_supported_metrics_return_in_one_pass():
    http = FakeHttp()
    values, dropped = await metrics.fetch_insights(http, "m1", "tok", ("reach",))
    assert values == {"reach": 12} and dropped == []
    assert len(http.attempts) == 1


async def test_an_unsupported_metric_is_dropped_and_the_rest_retried():
    """`impressions` was retired in favour of `views` on this account; the
    collector must lose that one name, not the whole snapshot."""
    http = FakeHttp(unsupported={"impressions"})
    values, dropped = await metrics.fetch_insights(
        http, "m1", "tok", ("reach", "impressions")
    )
    assert dropped == ["impressions"]
    assert "reach" in values and "impressions" not in values
    assert http.attempts[-1] == ["reach"]


async def test_several_unsupported_metrics_are_dropped_one_at_a_time():
    http = FakeHttp(unsupported={"impressions", "navigation"})
    _, dropped = await metrics.fetch_insights(
        http, "m1", "tok", ("reach", "impressions", "navigation")
    )
    assert set(dropped) == {"impressions", "navigation"}


async def test_an_error_that_names_no_metric_is_raised_not_looped():
    """Guards the retry loop from spinning on an auth failure."""

    class Dead:
        async def get_json(self, url, **kwargs):
            raise RuntimeError("400: token expired")

    with pytest.raises(RuntimeError):
        await metrics.fetch_insights(Dead(), "m1", "tok", ("reach",))


# ── row shaping ───────────────────────────────────────────────────────────────
def test_saved_is_stored_as_saves():
    row = metrics.to_row({}, {"saved": 4})
    assert row["saves"] == 4 and "saved" not in row


def test_node_like_count_wins_over_the_likes_insight():
    """The node field is what Instagram shows on the post itself, so a
    discrepancy should resolve to what a human would see."""
    row = metrics.to_row({"like_count": 9}, {"likes": 3})
    assert row["likes"] == 9


def test_raw_keeps_everything_so_a_metric_rename_is_not_data_loss():
    row = metrics.to_row({"permalink": "p"}, {"reach": 1, "brand_new_metric": 7})
    assert row["raw"]["insights"]["brand_new_metric"] == 7
    assert row["raw"]["node"]["permalink"] == "p"


# ── digest ────────────────────────────────────────────────────────────────────
def test_digest_reports_reach_against_the_recent_average():
    line = metrics.format_digest(
        "2026-09-03", {"reach": 120, "likes": 5}, {"reach": 100.0, "n": 7.0}
    )
    assert "120 reach" in line and "+20%" in line


def test_digest_omits_the_comparison_when_there_is_no_baseline():
    line = metrics.format_digest("2026-09-03", {"reach": 120}, None)
    assert "vs" not in line


def test_digest_renders_missing_metrics_as_a_dash_not_a_crash():
    assert "—" in metrics.format_digest("2026-09-03", {}, None)
