# ADR-0016: A whole-scene camera is frozen when its picture repeats; a line where nothing moves is held back, and cut after 300 s

- **Status:** **Proposed.** Built and tested on scripted frames and readers only (2026-10-27); nothing
  has run live. The owner decides `QUIET_S` and accepts the trade-off under Consequences (QUESTIONS.md
  Q-28).
- **Date:** 2026-10-27
- **Builds on:** [ADR-0014](0014-crossing-time-from-two-cameras.md). Its consequence "Stream failures are
  gaps" stands: a frozen picture is still a gap at its line, never zero cars. This ADR changes how a
  frozen picture is found on a camera with no burned-in clock, and adds a second kind of gap.
  [ADR-0015](0015-camera-wait-from-cars-ahead.md) is unchanged.
- **Changes:**
  - the whole-scene rule in `stream.frozen_check` (`b5fd8b4`, 2026-10-24): frozen when at most 10 % of
    the last 40 half-second pairs changed by 0.3 grey at 320×180;
  - the runner's fixed 20-s take-back, and its 10-s reopen after every `StreamFrozen`.

  The clock rule of the YouTube cameras is unchanged.
- **References:** QUESTIONS.md Q-28, Q-29 and Q-32; `docs/components/border-camera-reader.md`; the
  module docstrings of `stream.py` and `runner.py`.

## Context

One camera has no burned-in clock: the City's `city-pdn-north`, line B. Its frozen test is the whole
scene. Two runs on 2026-10-26 (local time) showed that test firing on a live picture:

| Run | Local time | Cuts at B | Rate | B unseen |
|---|---|---|---|---|
| The owner's `camera transit` | 17:30-19:07 (97.5 min) | 17, all at B | 10.5 an hour | 476 s, 8.1 % |
| A live check at `b173bc4` | 21:32-21:52 (20.2 min) | 17 `StreamFrozen`, merged into 12 cuts | 50.4 an hour (35.6 cuts) | 398 s for yolox, 32.8 % |

- **Every cut came from it: logged at night, inferred in the evening.** The live check logged each event:
  all 17 were `StreamFrozen` at B, and no StreamLost, expired segment, timeline cut, counter gap (past
  each line's start-up learning), or clock rule at A fired. The owner's records then carried no gap
  reason, so the evening's cause is read from B's unseen time. Every cut matched a jump in it, so none
  came from line A. 15 of 17 were 25.0-26.5 s, the size of one frozen gap (the 20-s take-back, the 5-s
  pad, about 1 s to reopen), which no other path leaves. 2 were 42.6 s and 49.0 s: a second freeze soon
  after reopening, or a slower reopen; the records cannot tell which.
- **The evening's cuts were not only at night.** 4 cuts in the 47 min of sun (17:41-18:11); 6 in 7 min at
  18:16-18:22, ending at sunset (18:22, computed for PDN); 3 in the 25 min of civil twilight; 4 in the 20
  min after dusk. That the cluster is the camera switching to a night mode is an inference, not measured.
- **The picture was live.** At night segments came every 5.0 s (243 of 243), and 0 of 2362 two-fps pairs
  were bit-identical (the smallest change was 0.0095 grey). At the native rate the picture changed at
  least every 0.42 s. It barely moved: the longest stretch with no pair at or above 0.3 grey was 16.5 s,
  yet the share of such pairs in a 20-s window fell as low as 2.5 %. No evening frame statistics were
  logged, so the evening's cuts are taken to be the same (ASSUMPTION).
- **Each reopen fired again.** `frozen_check` starts afresh with each read, and the new read's first
  20 s were judged on the same quiet road: 5 of the 17 night events fired at the first possible check.
  That is why 17 events made 12 cuts.
- **Anchoring needs a long uncut stretch.** An anchor sets the count on its own only when both ends of its
  trip lie inside the measured span with `HI_MIN` (90 min) to spare. So the first anchoring epoch must be
  longer than 180 − T min: about 174 min for a trip T of about 6 min (ASSUMPTION; T came from transits
  flagged under one anchor, so read it as 150-179 min). At 10.5 cuts an hour, 174 min uncut has a
  probability of about 6 × 10⁻¹⁴. The owner's run could not have anchored even with no cut (Q-29).

These figures come from one-off scripts over numbers-only logs (frame differences, segment times, the
records), not kept in the repository. No picture was kept or looked at.

## Decision

1. **A still tier in the reader (`stream.frozen_check`).** A camera without a clock region is frozen when
   at least 14 of the last 40 frame pairs are bit-identical at 320×180 (`FROZEN_SAME_SHARE`, a third of
   `FROZEN_SECONDS` = 20 s at 2 fps). It raises `StreamFrozen` ("is black", or "repeated exactly in N of
   the last 40 pairs", so the log shows how far past the share it was), and the stream is opened again.
   - **Why it fits a freeze.** An encoder fed one unchanging picture, or black, is expected to repeat it
     exactly between keyframes (ASSUMPTION from how H.264 codes an unchanged picture; no City freeze has
     been sampled). The chain does turn a repeated picture into identical frames: at the native rate,
     33.5 % of the City's frames repeated the one before.
   - **Its margin is thin per pair.** The reader's pairs are at least 0.48 s apart, against a longest
     unchanged native stretch of 0.42 s. So a whole scene is read at 2 fps or less (`SCENE_MAX_FPS`):
     `frozen_check` refuses a faster one with a ValueError, before any stream is opened.
   - **A third, not all,** leaves room for the pairs each keyframe changes. The share is a judgement
     (ASSUMPTION).
2. **A no-motion gap in the runner (`QUIET_S` = 300 s, ASSUMPTION).**
   - Each frame says whether it moved (`hls.Stamped.moved`): the clock ticked, or the whole scene changed
     by 0.3 grey. A read's first frame has no pair and says False. A reader with no frozen check says
     nothing (None), which counts as moved.
   - The runner keeps `moved_at` per camera. It is the runner's own, so it survives reopened streams. It
     is set on the camera's first frame, then moves forward on a frame that moved, or one in which a car
     was counted at that line.
   - When something moves at t1 and nothing had for more than 300 s, the stretch from `moved_at` to t1
     becomes a gap, "nothing moved for N s", before that frame reaches the counters. It raises no
     `StreamFrozen` and reopens nothing.
3. **A line is seen only up to its last moving frame.** `seen_until` is the older line's
   min(latest frame, `moved_at`). A still stretch is held, not written as empty road, because a written
   record is never corrected. If something moves within 300 s, the stretch is seen again as a quiet road;
   if not, decision 2 cuts it.
4. **The take-back is the rule's own.** `StreamFrozen.seconds` says how far back from the last frame the
   reader's rule judged (20 s today). The runner takes back exactly that.
5. **Reopening backs off.** After `StreamFrozen` after `StreamFrozen`, the wait before opening the stream
   again doubles: 10, 20, 40, 80, 160, then 300 s (`RECONNECT_MAX_S`). It drops back to 10 s only after a
   read whose frames reaching the runner span 60 s (`RESET_AFTER_S`, ASSUMPTION). A read that ends any
   other way leaves the wait as it is.
6. **Records name their gaps.** Each transit record carries `gaps_a` and `gaps_b`: each line's unseen
   stretches inside its span as the epochs were cut (padded, merged, clipped), with the minute each
   began, its length in seconds and its reasons. Runner records also carry `reconnects_a` and
   `reconnects_b`: how many times each line's stream was opened again since the runner started. Nothing
   per vehicle is added.
7. **A YouTube listing is dropped when its stream goes.** `stream.resolve` drops the channel's cached
   listing (`LISTING_TTL`, 600 s) when `yt-dlp -g` fails. The HLS reader drops it when the playlist is gone
   (404/410) or ended. A stream restarted under a new video id is then found on the next reopen, not up
   to 10 min later. A City URL is never in the listing, so this changes nothing at line B.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the 20-s share rule | At night it fired 50.4 times an hour on a picture measured live, and made every cut. In the evening it made 10.5 cuts an hour, attributed to it by their size (the records then gave no reason; that the picture was live is an ASSUMPTION). At that rate, the 174 min an anchor needs comes uncut with a probability of about 6 × 10⁻¹⁴ |
| The same rule with a lower bar (0.01 grey) | One live pair was already below 0.01, and 69 below 0.02 |
| A 300-s share tier in the reader: frozen when at most 10 % of 600 pairs move (the first proposal) | `frozen_check` starts afresh with each read, so a reopen restarts its window. With a reopen r s into a freeze, r − 10 s of it is never taken back (20-280 s for r from 30 to 290 s). Its 300-s take-back covers the freeze's start only when nothing reopens. Records are appended, never corrected, so what it misses stays written as empty road |
| A long window only, with no still tier | A freeze that repeats exactly would go unseen for 5 min |
| No frozen check on the City's stream | A stalled encoder would read as an empty road |
| `QUIET_S` 120 s | With that night's motion bursts thinned (ASSUMPTION: bursts scale with traffic), gaps came 0, 0.8 and 5.8 times an hour at full, half and a quarter of the traffic, against 0, 0 and 0.13 at 300 s |
| A hold with no gap | It ends as soon as something moves, so a still stretch of any length would then be seen as empty road |
| The still tier alone, with no hold | A freeze re-encoded with noise would read as empty road for as long as it lasted, and be written so |
| A native-frame checksum: ffmpeg's `showinfo` before `select` in `hls.Decoder`, "still" meaning no native frame changed for N s | **Deferred.** It would test the live 0.42-s maximum directly, but it changes how the Decoder's stderr is parsed. The installed ffmpeg prints a per-frame checksum (checked on a test source, not on the City stream) |
| No cut at a reopen seam that the replayed playlist covers | **Deferred.** The seam gap and its 5-s pad now hide the new reader's clock error: a fresh City clock is fitted from 2-3 Last-Modified samples, and fits from one read to the next differed by up to 0.52 s. A car in flight could be counted twice and the count stay sure. The old reader's clock must be carried into the new one first |
| No backoff | A picture frozen for good is opened again about every 30 s, each time fetching the playlist's segments again (about 0.26 MB on 2026-10-27) |
| Reset the backoff on any other end of a read | A StreamLost from the same stalled source would keep it at 10 s |
| Reconnects per record span | Spans overlap (each record covers up to 3 h), so per-span counts cannot be differenced. Cumulative counts give a rate from successive records |
| Keep a failed listing for its 600 s; or drop it on every `find()` miss too | Kept: a restarted YouTube stream stays dark for up to 10 min. On every miss: the channel is listed again on every reopen while a stream is down. The second is left open (Q-28) |

## Consequences

- **A freeze re-encoded with noise, shorter than 300 s, reads as a quiet road.** The old rule caught it in
  20-30 s. At about 3 cars a minute (ASSUMPTION), a 3-min freeze misses about 9 exits at B, more than the
  3 cars (`OUTLIER_CARS`) an anchor may disagree by. Inside a sure epoch the count then stays sure until
  anchors dispute it. A freeze that repeats exactly is still caught in 20 s. A noisy one longer than 300 s
  is cut from its last moving frame, across reopens.
- **"Now" lags by the last motion.** At line B at 21:30 traffic, the time from one moving pair to the next
  was p50 0.52 s, p99 8.4 s, at most 16.5 s. With that night's motion bursts thinned to a half and a
  quarter (ASSUMPTION), p99 was 49 s and 112 s, the longest 216 s and 407 s. Where nothing moves for
  minutes, as may happen in a standing queue (ASSUMPTION, not measured), the line stays minutes behind:
  stale, never an empty road.
  - The page then marks the camera wait "Stale" after 120 s (`LAG_ALERT_S`) although the stream is live.
    Its text names only a stream that stopped (Q-28).
  - The behind-live alert reads each line's latest frame, so the hold raises no alert.
- **A car counted in a frame whose picture barely changed,** after more than 300 s with nothing moving,
  ends that stretch with a gap that cuts the car too.
- **Black that is re-encoded with noise** is caught only by the no-motion gap, not as "is black" in 20 s.
- **A real recovery can wait up to 5 min** to be read again after a run of freezes. That stretch stays a
  gap.
- **Records grow by `gaps_a`, `gaps_b`, `reconnects_a` and `reconnects_b`.** A gap's start is cut to the
  minute, as the other written times are. Its reasons can name a segment number ("segment N expired
  before it was fetched"), never a car (Q-28).
- **A YouTube stream that is listed but refuses `-g`** ("technical difficulties") now costs one listing
  plus one `-g` per reopen, instead of one `-g`.
- **Anchoring is unchanged.** It still needs about 174 min uncut at both lines. And since every enclosed
  anchor left B more than `CHECK_MAX_MIN` (60 min) before the end, "now" stays flagged unless A and B
  capture agree to about 1 % (Q-29).
- **Verification is owed** (Q-32): runs at dusk straddling sunset, deep night, peak, and one under
  `camera estimate`, with numbers-only logging. Until then the still tier's margin, `QUIET_S` and the lag
  rest on one 20-min night sample at line B.
- **Tests** on 2026-10-27, on scripted frames and readers: `test_border_camera_stream.py` 29,
  `_hls.py` 33, `_runner.py` 46, `_transit.py` 44. A copy of the runner and transit with each new
  behaviour broken in turn failed the tests 11 times out of 11.
