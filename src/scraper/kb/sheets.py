"""Write the export into a Google Sheet.

Auth is a service account: create one in Google Cloud, enable the Sheets API,
and share the target sheet with the service account's email as an Editor. That
last step is the one people miss — a service account has no access to a sheet
until it is shared like any other collaborator.

``gspread`` is an optional dependency (the ``kb`` extra) so the base install and
the scrape job stay untouched by it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..core.config import settings

log = logging.getLogger("scraper.kb")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsUnavailable(RuntimeError):
    """gspread isn't installed, or no usable credentials were configured."""


def _credentials() -> Any:
    try:
        from google.oauth2.service_account import Credentials
    except ImportError as exc:  # pragma: no cover - import guard
        raise SheetsUnavailable(
            "google-auth is not installed. Install the extra: pip install -e '.[kb]'"
        ) from exc

    raw = settings.google_service_account_json
    if raw:
        # GitHub Actions secrets are strings, so the whole JSON key file travels
        # as one env var rather than as a path to a file that doesn't exist on
        # the runner.
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SheetsUnavailable(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON"
            ) from exc
        return Credentials.from_service_account_info(info, scopes=_SCOPES)

    path = settings.google_application_credentials
    if path:
        return Credentials.from_service_account_file(path, scopes=_SCOPES)

    raise SheetsUnavailable(
        "Set GOOGLE_SERVICE_ACCOUNT_JSON (the key file's contents) or "
        "GOOGLE_APPLICATION_CREDENTIALS (a path to it)."
    )


def _open_worksheet(sheet_id: str, tab: str) -> Any:
    try:
        import gspread
    except ImportError as exc:  # pragma: no cover - import guard
        raise SheetsUnavailable(
            "gspread is not installed. Install the extra: pip install -e '.[kb]'"
        ) from exc

    spreadsheet = gspread.authorize(_credentials()).open_by_key(sheet_id)
    try:
        return spreadsheet.worksheet(tab)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab, rows=1, cols=1)


def write_sheet(
    values: list[list[str]],
    *,
    sheet_id: Optional[str] = None,
    tab: Optional[str] = None,
) -> int:
    """Replace the worksheet's contents with `values`. Returns rows written.

    Deliberately NOT clear-then-write: a clear leaves the sheet empty for the
    length of a round trip, and an import that lands in that window would wipe
    the knowledge base. Writing first and shrinking after means the sheet is
    never emptier than it has to be — the worst case is a few stale trailing
    rows for one round trip, which is a far cheaper failure.
    """
    sheet_id = sheet_id or settings.kb_sheet_id
    tab = tab or settings.kb_sheet_tab
    if not sheet_id:
        raise SheetsUnavailable("KB_SHEET_ID is not set.")

    worksheet = _open_worksheet(sheet_id, tab)
    needed_rows = len(values)
    needed_cols = max(len(r) for r in values)

    # Grow first — an update past the current grid bounds is an API error.
    if worksheet.row_count < needed_rows or worksheet.col_count < needed_cols:
        worksheet.resize(
            rows=max(worksheet.row_count, needed_rows),
            cols=max(worksheet.col_count, needed_cols),
        )

    worksheet.update(values, "A1", value_input_option="RAW")

    if worksheet.row_count > needed_rows:
        worksheet.resize(rows=needed_rows, cols=needed_cols)

    log.info("wrote %d rows to sheet %s tab %s", needed_rows - 1, sheet_id, tab)
    return needed_rows - 1  # minus the header
