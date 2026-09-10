# Testing

239 tests, collected in under a second. **None of them run in CI** — see
[known-gaps.md](../known-gaps.md). Run them locally; there is no safety net.

---

## Layout

```
tests/
├── kb/       test_rows.py                        15 tests
├── social/   14 files                           209 tests
└── sources/  test_events_web_time.py
             test_ticketmaster_paging.py          15 tests
```

- **No `conftest.py` anywhere, and no `__init__.py`.** There is no shared fixture module — every file
  is self-contained.
- No `testpaths` config, so bare `pytest` from the repo root discovers all three directories.
  `scraper` resolves because the package is installed editable.
- Files mirror the **module** under test, not the feature.

The distribution is worth noting: the social pipeline has 209 tests and `core/` has effectively none.
That is inverted relative to blast radius — `core/` is what every surface depends on.

---

## Commands

```bash
pip install -e ".[dev]"       # ruff, pytest, pytest-asyncio
pip install -e ".[social]"    # Pillow — REQUIRED or ~9 social test files fail to import

pytest                        # all 239
pytest tests/kb -q            # 15  — KB export row rules
pytest tests/social -q        # 209 — carousel pipeline
pytest tests/sources -q       # 15  — connector time and paging
pytest tests/kb/test_rows.py::test_no_cell_is_ever_empty
pytest -k "plausible" -q

ruff check .
```

There is no test script at either level, and **no test suite at all for the web app.**

> `ruff` and `pytest` are not necessarily installed in a checked-in `.venv` — install the `dev` extra
> first.

---

## Conventions

1. **`asyncio_mode = "auto"`** — write `async def test_…` with **no** decorator.
2. **`from __future__ import annotations`** at the top of every test file, without exception.
3. **A module docstring naming the concrete failure the file prevents** — ideally the real incident,
   with measured numbers and dates. This is the strongest convention in the repo. Examples worth
   copying: `test_ticketmaster_paging.py:1-8`, `test_metrics.py:1-8`.
4. **Long declarative test names**: `test_an_unsupported_metric_is_dropped_and_the_rest_retried`,
   `test_implausible_time_is_omitted_not_guessed`, `test_a_pending_edit_holds_the_post_back`.
5. **`monkeypatch.setattr(settings, …)`** for configuration — never environment mutation, because
   `settings` is an import-time snapshot.
6. **Module-level constants for fixture data**, then a local `build(**overrides)` helper that spreads
   with overrides.
7. **Index by name, never by position** — `cell(row, "venue")` via `HEADERS.index(name)`. This is what
   lets the KB sheet's headers stay append-only without rewriting assertions.
8. **Never depend on the current wall-clock hour.** Use a day fully in the future or fully in the past.
9. `@pytest.mark.parametrize` for families of inputs.

---

## What each notable file pins

| File | Invariant |
|---|---|
| `kb/test_rows.py` | The whole KB row contract: bilingual prose in one cell, **no relative dates in either language**, ticket link beats source URL, joined venue beats raw columns, an implausible 3am time is omitted while the date survives, boilerplate titles dropped, **no cell is ever empty**, `"Not listed"` never reaches the prose cell |
| `social/test_post_kinds.py` | All bounds functions, `period_key`, profile weights, the `_variant_for` modulus, **digest byte-identity across the kinds change**, and the three horizon venue-cap regressions |
| `social/test_slide_layout.py` | "Never ellipsise a title", `fit_block` behaviour, band shaping per source aspect, mount-not-blur, both extreme edges preserved |
| `social/test_apply_edits.py` | Drop rebuilds, cover re-render, orphan removal, stored-order re-imposition, below-minimum refusal, request ordering, mid-rebuild approval abandon, custom-caption survival |
| `social/test_autoapprove.py` | The opt-out sweep: approve/expire/future/race/idempotence/disabled/inert, the stale-backlog guard, the pending-edit hold, alert-once |
| `social/test_metrics.py` | Credential redaction (four params, error body preserved) and all four branches of the degrade-and-retry loop |
| `social/test_publish.py` | `token_expires_in_days` must **propagate** an introspection failure, not swallow it to `None` |
| `social/test_notify.py` | The cross-language HMAC review-token contract |
| `social/test_telegram.py` | `ok=false` despite HTTP 200; the token never leaks into an error; redirect refusal |
| `social/test_scheduling.py` | **Parses `ig_daily.yml`** and asserts the sweep window matches the declared one, and that the deadline falls inside it year-round |
| `social/test_event_times.py` | The timestamp invariant: naive vs aware, DST per date, venue-key collapsing |
| `sources/test_events_web_time.py` | JSON-LD start times: date-only listings, and an offset that contradicts the venue |
| `sources/test_ticketmaster_paging.py` | Pagination — date-ascending results and a 200 cap mean a single request drops the furthest-out events |

Note `test_scheduling.py` is unusual and worth imitating: **it tests a config file, not code.** The
scheduling invariant lives in YAML, so that is where the test reaches.

---

## Fakes to copy

There is no shared fixtures package, so these are **copy-and-adapt patterns, not imports.**

| Fake | Where | Shape |
|---|---|---|
| `FakeStorage` (rebuild flavour) | `test_apply_edits.py:36-68` | Async methods mirroring the real `Storage` surface. Two details worth copying: `events_by_ids` returns rows **deliberately reversed**, because the real method makes no order guarantee and the rebuild must re-impose the stored order; and a `cas_succeeds` flag simulates a lost compare-and-swap |
| `FakeStorage` (sweep flavour) | `test_autoapprove.py:18-` | A second, smaller stand-in. Two coexist precisely because there is no shared module — match the one whose surface you need |
| `FakeHttp` | `test_metrics.py:53-75` | *"Rejects any metric named in `unsupported`, the way Meta does."* Records every attempt so a test can assert the **shape of the degrade loop**, and raises an error string reproducing Meta's real 400 body. The model for any Graph-facing test |
| `wired` fixture | `test_apply_edits.py:71-116` | The canonical harness: monkeypatch `settings` attributes, swap `upload_slides`/`remove_objects` to capture rather than perform, stub `fetch_photo`, collect notifications, and swap `social.Storage` for the fake. Returns an `_install(...)` closure so each test builds its own post row |

---

## Coverage gaps

**Untested and load-bearing:**

| Area | Why it matters |
|---|---|
| `core/orchestrator.py` | The one path every caller goes through |
| `core/dedupe.py` | Threshold changes have no regression net; a false merge silently hides a real event |
| `core/storage.py` | The cross-run merge and every read path |
| `core/geocode.py`, `core/config.py`, `sources/registry.py` | — |
| Every directory parser | The most fragile code in the repo — CSS selectors and regexes against third-party HTML |
| `slides_store.py` | No test file at all |
| `publish_carousel`'s child-skip and `min_children` logic | Only the token helpers are tested |
| The `address_hash` three-way parity | Three implementations that must be byte-identical, with nothing pinning it |
| The entire web app | No test framework configured |

**Where to add a test first**, ranked by value:

1. A parity test for `address_hash` across Python and SQL — cheap, and it pins a genuine cross-language
   contract.
2. `dedupe.py` threshold behaviour, using the real title pairs quoted in its comments (the Salsa/
   Bachata vs Machetes examples are already worked out).
3. `orchestrator.run` failure isolation — assert one raising source does not fail the run.
4. `storage._apply_merge`'s never-overwrite rule.

**Until CI runs any of this**, the practical rule is the one in
[conventions](conventions.md#the-verification-contract): validate against production data with
`--dry-run`, and look at the rendered output.

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
