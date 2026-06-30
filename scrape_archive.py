"""
Playwright scraper for the Tel Aviv Engineering Archive permit timeline.

URL pattern:
  https://handasa.tel-aviv.gov.il/Pages/SearchResultsAnonPageNew.aspx
  ?partialAddress={k_rechov}_{ms_bayit}

The page is an AngularJS SPA on SharePoint.  We wait for network-idle,
then extract permit-stage rows that contain a Hebrew milestone label and
a date string.

Returned dict keys:
  form1          – טופס 1 / פתיחת בקשה
  permit_verbal  – היתר מילולי חתום
  permit_signed  – תכנית חתומה / היתר — תכנית חתומה
  build          – תחילת בנייה
  form4          – טופס 4 / אכלוס
"""

import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

_TIMEOUT_MS = 25_000  # allow Angular + SharePoint a generous budget

# Hebrew label fragments → our internal timeline key.
# Ordered from most-specific to least-specific so longer phrases match first.
_LABEL_KEY: list[tuple[str, str]] = [
    ("היתר מילולי חתום",    "permit_verbal"),
    ("היתר מילולי",         "permit_verbal"),
    ("תכנית חתומה",         "permit_signed"),
    ("היתר - תכנית חתומה",  "permit_signed"),
    ("היתר—תכנית חתומה",    "permit_signed"),
    ("התחלת בניה",          "build"),
    ("תחילת בנייה",         "build"),
    ("תחילת בניה",          "build"),
    ("טופס 4",              "form4"),
    ("טופס אכלוס",          "form4"),
    ("אכלוס",               "form4"),
    ("טופס 1",              "form1"),
    ("בקשת רישוי",          "form1"),
    ("פתיחת בקשה",          "form1"),
]

_DATE_RE = re.compile(r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})\b")


def _parse_date(text: str) -> str | None:
    """Parse DD/MM/YYYY (or DD.MM.YYYY / DD-MM-YYYY) → ISO date string."""
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date().isoformat()
    except ValueError:
        return None


def _label_for(text: str) -> str | None:
    for fragment, key in _LABEL_KEY:
        if fragment in text:
            return key
    return None


def _extract_from_rows(rows) -> dict:
    """
    Given a list of Playwright element handles for <tr> elements, return a
    {key: iso_date} dict for any row that contains both a milestone label
    and a date string.
    """
    found: dict[str, str] = {}
    for row in rows:
        cells = row.query_selector_all("td, th")
        texts = [c.inner_text().strip() for c in cells]
        row_text = " ".join(texts)
        key = _label_for(row_text)
        if not key:
            continue
        date = _parse_date(row_text)
        if date and key not in found:
            found[key] = date
    return found


def _extract_from_flat_text(text: str) -> dict:
    """
    Fallback: scan the full page text line-by-line for a label followed
    (within ~120 chars) by a date.
    """
    found: dict[str, str] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        key = _label_for(line)
        if not key or key in found:
            continue
        # Check the same line and the next 3 lines for a date
        window = " ".join(lines[i : i + 4])
        date = _parse_date(window)
        if date:
            found[key] = date
    return found


def scrape_archive_timeline(archive_url: str) -> dict:
    """
    Load the Tel Aviv engineering archive page and return
    {milestone_key: iso_date_str}.  Returns {} on any failure.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # lazy import

    timeline: dict[str, str] = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                locale="he-IL",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            page.goto(archive_url, timeout=_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=_TIMEOUT_MS)
            except PWTimeout:
                pass  # capture whatever rendered so far

            # ── Strategy 1: table rows ──────────────────────────────────────
            rows = page.query_selector_all("tr")
            if rows:
                timeline = _extract_from_rows(rows)

            # ── Strategy 2: flat text fallback ─────────────────────────────
            if not timeline:
                body_text = page.inner_text("body")
                timeline = _extract_from_flat_text(body_text)

            browser.close()
    except Exception as exc:
        log.warning("Archive scrape failed (%s): %s", archive_url, exc)

    log.info("Archive scrape %s → %s", archive_url, timeline)
    return timeline
