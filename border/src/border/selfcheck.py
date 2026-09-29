"""Check the live feed still looks like the feed we parse.

Unit tests run against saved responses, so they keep passing while CBP quietly renames
a field and every post goes empty. This asks the real thing and fails loudly.

    python3 -m border.selfcheck            # exits 1 on any problem
    python3 -m border.selfcheck --json     # for a monitor to read

Run it after deploying, and on a schedule if anything depends on this feed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .core import cbp, text
from .core.storage import Storage
from .service import BorderFeed

# Fields we actually read. A rename here is the failure this catches.
PORT_FIELDS = ("port_number", "port_status", "hours", "crossing_name")
LANE_GROUPS = {
    "passenger_vehicle_lanes": ("standard_lanes", "NEXUS_SENTRI_lanes", "ready_lanes"),
    "pedestrian_lanes": ("standard_lanes", "ready_lanes"),
    "commercial_vehicle_lanes": ("standard_lanes", "FAST_lanes"),
}
LANE_FIELDS = ("operational_status", "delay_minutes", "update_time", "lanes_open")
KNOWN_STATUSES = {"no delay", "delay", "lanes closed", "update pending", "n/a", ""}


class Check:
    def __init__(self):
        self.problems: list[str] = []
        self.notes: list[str] = []

    def fail(self, message: str) -> None:
        self.problems.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_feed(payload: list[dict], check: Check) -> None:
    ours = [p for p in payload if p.get("port_number") in cbp.PORTS]
    missing = set(cbp.PORTS) - {p.get("port_number") for p in ours}
    if missing:
        check.fail(f"CBP no longer lists {len(missing)} of our ports: {sorted(missing)}")

    for port in ours:
        where = port["port_number"]
        for field in PORT_FIELDS:
            if field not in port:
                check.fail(f"{where}: port field '{field}' is gone")
        for group, lanes in LANE_GROUPS.items():
            if group not in port:
                check.fail(f"{where}: lane group '{group}' is gone")
                continue
            for lane in lanes:
                if lane not in port[group]:
                    check.fail(f"{where}: lane '{group}.{lane}' is gone")
                    continue
                for field in LANE_FIELDS:
                    if field not in port[group][lane]:
                        check.fail(f"{where}: field '{group}.{lane}.{field}' is gone")
                status = str(port[group][lane].get("operational_status", "")).lower()
                if status not in KNOWN_STATUSES:
                    check.fail(f"{where}: unknown status '{status}' in {group}.{lane}")


def check_parsing(check: Check, payload: list[dict] | None = None) -> dict:
    """Parse for real and confirm the result is usable, not just well-shaped."""
    # Reuse the payload check_feed just inspected: one CBP read, and both checks judge
    # the same response.
    fetcher = (lambda: payload) if payload is not None else cbp.fetch
    feed = BorderFeed(storage=Storage(url="", key=""), fetcher=fetcher)
    snapshot = feed.snapshot()
    health = feed.health()
    summary: dict = {"ports": len(snapshot.ports), "sources": health["sources"]}

    if len(snapshot.ports) != len(cbp.PORTS):
        check.fail(f"parsed {len(snapshot.ports)} ports, expected {len(cbp.PORTS)}")

    unnamed = [p.port_number for p in snapshot.ports if not p.name]
    if unnamed:
        check.fail(f"ports with no name: {unnamed}")

    live = [p for p in snapshot.ports if p.has_live_data]
    summary["ports_with_data"] = len(live)
    if not live:
        check.fail("no bridge reported a usable number — CBP may be down or changed")
    elif len(live) < 3:
        check.note(f"only {len(live)} of {len(snapshot.ports)} bridges are reporting")

    stamps = [l.cbp_updated_at for p in snapshot.ports for l in p.lanes.values()
              if l.state == "open" and l.cbp_updated_at]
    if stamps and not any(l.age_minutes is not None for p in snapshot.ports
                          for l in p.lanes.values() if l.state == "open"):
        check.fail("no update time could be parsed — the stamp format may have changed")

    best = feed.best("car", snapshot)
    summary["best_car"] = f"{best['name']} {best['minutes']} min" if best else None
    if best is None:
        check.note("no car lane is open anywhere right now")

    caption = text.caption(feed, snapshot, "es")
    summary["caption_lines"] = len(caption.splitlines())
    if len(caption) < 40:
        check.fail("the Spanish caption came out empty or nearly so")

    # A site being down is weather; a site parsing to nothing, or losing a bridge, is
    # the page changing shape under our scraper, which is exactly what this is for.
    for source, status in feed.source_status.items():
        if status.state == "failed":
            check.note(f"source {source} is {status}")
        elif status.problem:
            check.fail(f"source {source} is {status}")
    mirror = health["parser_check"]
    summary["parser_check"] = {k: mirror[k] for k in ("compared", "skipped_other_update")}
    for mismatch in mirror["mismatches"]:
        check.fail(f"a CBP mirror disagrees with our parsing: {mismatch}")
    return summary


def run() -> tuple[Check, dict]:
    check = Check()
    summary: dict = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        payload = cbp.fetch()
        summary["ports_in_feed"] = len(payload)
        check_feed(payload, check)
    except Exception as exc:
        check.fail(f"could not read the CBP feed: {type(exc).__name__}: {exc}")
        return check, summary

    try:
        summary.update(check_parsing(check, payload))
    except Exception as exc:
        check.fail(f"parsing the feed raised: {type(exc).__name__}: {exc}")
    return check, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    check, summary = run()
    if args.json:
        print(json.dumps({"ok": check.ok, "problems": check.problems,
                          "notes": check.notes, **summary}, indent=2, ensure_ascii=False))
    else:
        print(f"checked {summary['checked_at']}")
        for key, value in summary.items():
            if key != "checked_at":
                print(f"  {key}: {value}")
        for note in check.notes:
            print(f"  note: {note}")
        for problem in check.problems:
            print(f"  PROBLEM: {problem}", file=sys.stderr)
        print("\nok" if check.ok else f"\n{len(check.problems)} problem(s)")
    return 0 if check.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
