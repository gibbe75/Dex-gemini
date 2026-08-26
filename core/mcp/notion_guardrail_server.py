#!/usr/bin/env python3
"""Notion guardrail MCP server.

Exposes exactly 5 tools, each hardcoded to a single Notion database, with
guardrails enforced in code (never left to the model's judgment):

  - lister_elements_dex     : read-only, DB1 "Dex" view (Status actif seulement)
  - creer_element_db        : DB1, force Source="Dex", statut restreint aux actifs
  - modifier_element_db     : DB1, verifie Source=="Dex" avant tout ecriture
  - supprimer_element_db    : DB1, verifie Source=="Dex", passe Status="Done"
  - lire_reunions_date      : lecture seule, DB2, une date exacte, jamais de plage

No generic navigate/click tool is exposed — only these 5 named actions.
Notion API/MCP is not usable in this environment (hard policy block), so
this drives a real, persistently-authenticated Chrome session via
Playwright instead.

IMPORTANT: the DOM interactions below (clicking "New page", opening the
Source/Status dropdowns, switching the Notes/Transcript tabs) are a
first-pass best effort, NOT live-tested against the real Notion UI the
way the authentication flow was. They will very likely need live
debugging the same way login did — see core/mcp/notion_guardrail/discover_schema.py
for the harness used to do that kind of iteration.
"""
import asyncio
import calendar
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from playwright.async_api import Page, async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "notion_guardrail" / "browser_profile"

# --- Hardcoded, locked-down targets -----------------------------------
# DB1: personal backlog ("0 - To do"), view "Dex"
DB1_CONTAINER_PAGE_ID = "225d278ab26e804b8d1bfe1de87ddad5"
DB1_DEX_VIEW_TAB_TEXT = "Dex"
DB1_DATABASE_ID = "246d278a-b26e-8168-87d0-000b2cf6bfb7"

# DB2: "Transcripts meetings" (AI Meeting Notes)
DB2_DATABASE_ID = "3c2d278a-b26e-80d4-9199-000b04f9e2c7"
DB2_CONTAINER_PAGE_ID = "3c2d278ab26e802c9ebeccf61e504854"

SOURCE_PROPERTY = "Source"
SOURCE_VALUE = "Dex"
STATUS_PROPERTY = "Status"
ACTIVE_STATUSES = ["Backlog", "On-hold", "Demain", "Today", "In progress"]
DONE_STATUS = "Done"

# Every "id" this server hands out or accepts is this property's value
# (e.g. "DEX-5", "MTG-19") — Notion's native Unique ID property, added to
# both databases specifically so this server never has to scrape a page id
# out of a URL again (confirmed unreliable: regex boundary bugs, and a
# race between pressing Enter on a search result and page.url updating).
# It also directly resolves as a Notion URL: notion.so/{WORKSPACE_SLUG}/{id}
# — confirmed live, redirects straight to the target page, no search needed.
ID_PROPERTY = "id_auto"
WORKSPACE_SLUG = "luccasoftware"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

app = Server("notion-guardrail-mcp")

# --- Shared, long-lived browser session --------------------------------
# One persistent context for the whole server lifetime — every tool call
# reuses it. _lock serializes calls: Playwright automation is inherently
# stateful (one page, one place at a time), never run two calls at once.
_pw = None
_context = None
_page: Optional[Page] = None
_lock = asyncio.Lock()


async def _get_page() -> Page:
    global _pw, _context, _page
    if _page is not None:
        return _page
    _pw = await async_playwright().start()
    _context = await _pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=True,
        channel="chrome",
        viewport={"width": 1440, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    await _context.grant_permissions(["clipboard-read", "clipboard-write"])
    _page = _context.pages[0] if _context.pages else await _context.new_page()
    return _page


def _notion_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def _notion_direct_url(id_auto_value: str) -> str:
    """The Unique ID property (id_auto) resolves as its own Notion URL —
    confirmed live: notion.so/{WORKSPACE_SLUG}/DEX-5 redirects straight to
    that row's real page, no search/indexing-lag involved."""
    return f"https://app.notion.com/p/{WORKSPACE_SLUG}/{id_auto_value}"


async def _read_id_auto(page: Page, scope) -> str:
    value = await _get_property_value_text(page, scope, ID_PROPERTY)
    if not value:
        raise RuntimeError(
            f"Impossible de lire la propriete '{ID_PROPERTY}' sur {page.url!r}."
        )
    return value


async def _open_by_id_auto(page: Page, id_auto_value: str) -> None:
    """Notion resolves this as a same-origin client-side redirect (no full
    document reload), so waiting on Playwright's default "load" event can
    hang past a page.goto timeout even though the page itself navigates
    fine — "domcontentloaded" is the condition that actually fires here.
    One retry on timeout: observed occasionally slow against Notion's own
    background traffic, same as every other navigation in this file."""
    url = _notion_direct_url(id_auto_value)
    for attempt in range(2):
        try:
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            break
        except Exception:
            if attempt == 1:
                raise
    await _settle(page)


async def _settle(page: Page) -> None:
    # Notion keeps background network traffic (websockets, polling) alive
    # essentially forever, so "networkidle" almost never fires — treat a
    # timeout here as normal, not an error.
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)


async def _open_item(page: Page, page_id: str) -> None:
    await page.goto(_notion_url(page_id), timeout=30000)
    await _settle(page)


async def _active_scope(page: Page):
    """A newly created row sometimes opens in a side peek/overlay panel,
    layered on top of the underlying page — interactions must then target
    elements inside that panel, not the (still-present) page behind it.
    Notion always keeps a '.notion-overlay-container' mounted even with no
    peek open, so presence alone is not enough — it must also actually be
    visible with real size. Returns the open overlay's locator only then,
    else the page itself (also the case for modifier_element_db/
    supprimer_element_db, which navigate directly to a page — no peek)."""
    overlay = page.locator(".notion-overlay-container").last
    if await overlay.count() == 0:
        return page
    try:
        if not await overlay.is_visible():
            return page
        box = await overlay.bounding_box()
    except Exception:
        return page
    if not box or box["width"] < 100 or box["height"] < 100:
        return page
    try:
        has_editable = await overlay.locator('[contenteditable="true"]').count() > 0
    except Exception:
        has_editable = False
    return overlay if has_editable else page


async def _find_property_value_cell(scope, prop_label: str):
    """The property panel's label and value are NOT simple DOM siblings —
    the label text sits 6+ wrapper levels deep inside a 2-column row
    (confirmed by direct inspection: walking up from the label, the first
    ancestor whose own parent has exactly 2 children is that row; its
    second child is the value cell). Returns that value cell as an
    ElementHandle, or None if not found.

    Must search within `scope`'s own subtree, not the whole document —
    confirmed live: when `scope` is an open center-peek, the underlying
    table (with its own "Source"/"Status" column headers) is still
    present in the DOM behind it, and an unscoped whole-document search
    silently grabbed that background cell instead of the peek's own
    property row, then hung retrying a click blocked by the peek's own
    backdrop ("subtree intercepts pointer events")."""
    js_body = """
        const label = Array.from(root.querySelectorAll('*')).find(
            el => el.textContent.trim() === propLabel && el.children.length === 0
        );
        if (!label) return null;
        let node = label;
        for (let i = 0; i < 12 && node; i++) {
            const parent = node.parentElement;
            if (parent && parent.children.length === 2) {
                return parent.children[1];
            }
            node = parent;
        }
        return null;
    """
    if isinstance(scope, Page):
        handle = await scope.evaluate_handle(
            "(propLabel) => { const root = document; %s }" % js_body,
            prop_label,
        )
    else:
        handle = await scope.evaluate_handle(
            "(root, propLabel) => { %s }" % js_body,
            prop_label,
        )
    return handle.as_element()


async def _get_property_value_text(page: Page, scope, prop_label: str) -> str:
    """Best-effort read of a page property's current value, by its label."""
    cell = await _find_property_value_cell(scope, prop_label)
    if cell is None:
        return ""
    try:
        return (await cell.inner_text()).strip()
    except Exception:
        return ""


async def _set_select_property(page: Page, scope, prop_label: str, option_text: str) -> None:
    """Open a select/status property panel and click the option matching
    option_text exactly (never free-typed — always chosen from the
    existing dropdown, so a select field can never gain a new option by
    accident)."""
    # Click the VALUE cell next to the label, not the label itself — the
    # label opens the property's own admin menu (Rename/Delete property/...)
    # instead of the value picker.
    value_cell = await _find_property_value_cell(scope, prop_label)
    if value_cell is None:
        raise RuntimeError(f"Cellule de valeur introuvable pour '{prop_label}'.")
    await value_cell.click()
    option = page.get_by_role("option", name=option_text, exact=True).first
    await option.wait_for(timeout=5000)
    await option.click()
    # Deliberately no Escape here: on the new-row peek, Escape closes the
    # whole peek (not just the option dropdown) and breaks later calls
    # that still expect it open.


async def _verify_source_is_dex(page: Page, scope) -> None:
    value = await _get_property_value_text(page, scope, SOURCE_PROPERTY)
    if SOURCE_VALUE not in value:
        raise PermissionError(
            f"Cette ligne n'a pas {SOURCE_PROPERTY}={SOURCE_VALUE} "
            f"(valeur lue: {value!r}) — refus de modifier/supprimer."
        )


async def _set_title(page: Page, scope, titre: str) -> None:
    title_block = scope.locator(
        'h1[contenteditable="true"], [role="textbox"][contenteditable="true"]'
    ).first
    await title_block.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.type(titre, delay=30)
    # Deliberately no Escape/Tab here: on the new-row peek, Escape closes
    # the whole peek. The next click (on another field) blurs this one.


async def _set_body_content(page: Page, scope, contenu: str, replace: bool) -> None:
    """Write contenu into the page body (below the properties), replacing
    any existing body content when replace=True."""
    body = scope.locator("div.notion-page-content")
    await body.click()
    if replace:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
    for line in contenu.split("\n"):
        # A short per-character delay — typing at Playwright's default
        # speed dropped characters against Notion's editor in testing
        # (observed: "Contenu de test final 9." landed as "Contenu de").
        await page.keyboard.type(line, delay=30)
        await page.keyboard.press("Enter")
    # Deliberately no Escape here either — see _set_title.


# --- Tool implementations -----------------------------------------------

async def _ensure_dex_view(page: Page) -> None:
    """Switch to the 'Dex' view tab only if it isn't already active.
    Clicking an already-active Notion view tab opens its settings menu
    (Rename/Delete view/...) instead of doing nothing — always dismiss
    with Escape right after a real click, to avoid leaving that menu
    open and blocking later clicks (e.g. on 'New page')."""
    tab = page.get_by_role("tab", name=DB1_DEX_VIEW_TAB_TEXT, exact=True)
    if await tab.count() == 0:
        tab = page.get_by_text(DB1_DEX_VIEW_TAB_TEXT, exact=True)
    selected = await tab.first.get_attribute("aria-selected")
    if selected != "true":
        await tab.first.click()
        await page.keyboard.press("Escape")
        await _settle(page)


def _notion_date_display(date_str: str) -> str:
    """Notion renders its Date property as e.g. 'August 25, 2026' in this
    workspace, regardless of the French UI locale elsewhere."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{calendar.month_name[dt.month]} {dt.day}, {dt.year}"


async def _search_and_open(page: Page, titre: str, max_attempts: int = 15) -> bool:
    """The only navigation method proven reliable for reaching a row's
    real full page: a single click on a table row's title cell was
    confirmed UNRELIABLE (sometimes edits, sometimes navigates, on both
    databases, with no discernible pattern) — Notion's own quick-find
    search is what actually works consistently. For content that already
    existed before this call (not something just created a moment ago),
    it should already be indexed, so this is fast — no need for the long
    retry budget creer_element_db needs for brand new rows."""
    dialog = page.locator('[role="dialog"]')
    result = dialog.get_by_text(titre, exact=True).first
    await page.keyboard.press("Control+P")
    await page.wait_for_timeout(400)
    await page.keyboard.press("Control+A")
    await page.keyboard.type(titre, delay=20)
    found = False
    for _ in range(max_attempts):
        await page.wait_for_timeout(500)
        if await result.count() > 0:
            found = True
            break
    if not found:
        await page.keyboard.press("Escape")
        return False
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1200)
    return True


async def _open_row_via_hover(page: Page, row_title_text: str, occurrence: int = 0) -> bool:
    """Hovering the right edge of a table row's Name cell reveals an
    "Open" affordance (confirmed live, via a screenshot from the user —
    a direct click on the row title itself only toggles inline rename,
    this hidden button is the real one) that opens the row as a full
    center-peek overlay: the complete property panel (Status, Source,
    id_auto, ...) plus an editable body, not a lightweight preview.
    Unlike _search_and_open, this never depends on Notion's search index
    — it acts on a row already rendered in the currently loaded table, so
    it works immediately after creating a row, with no indexing lag.

    A title that was already opened once also starts appearing in the
    left sidebar's "Recents" list — confirmed live via a screenshot: once
    that happens, a plain .first match against the exact title text picks
    the sidebar link instead of the table row (it sits earlier in the
    DOM), and hovering it never reveals an "Open" button at all. The
    sidebar is confirmed to occupy roughly the left 300px consistently —
    skip any match found there rather than trusting DOM order.

    `occurrence` (0-indexed, among matches outside the sidebar) picks a
    specific row when several share the exact same title — confirmed
    live: a recurring meeting (e.g. a daily 1:1) reuses the same title
    across many rows, and opening "by title alone" always resolved to
    whichever same-titled row came first, silently returning the wrong
    date's meeting."""
    candidates = page.get_by_text(row_title_text, exact=True)
    count = await candidates.count()
    row = None
    seen = 0
    for i in range(count):
        b = await candidates.nth(i).bounding_box()
        if b and b["x"] > 300:
            if seen == occurrence:
                row = candidates.nth(i)
                break
            seen += 1
    if row is None:
        return False
    await row.scroll_into_view_if_needed()
    await page.wait_for_timeout(200)

    open_btn = None
    best_i = 0
    # The hover affordance has proven flaky against automated mouse.move
    # (worked first try in manual testing, silently absent on some later
    # attempts) -- retry the hover-in/hover-out sequence a few times
    # rather than giving up on the first miss.
    for attempt in range(4):
        box = await row.bounding_box()
        if not box:
            return False
        # Move through the row first, then out past its right edge -- the
        # affordance only renders on a real hover transition, not a jump.
        await page.mouse.move(box["x"] + 5, box["y"] + box["height"] / 2)
        await page.wait_for_timeout(200)
        await page.mouse.move(box["x"] + box["width"] + 80, box["y"] + box["height"] / 2, steps=10)
        await page.wait_for_timeout(600 + attempt * 300)

        # Rendered visually as "OPEN" (CSS uppercase) but the actual text
        # node is "Open" — an exact-case match silently finds nothing.
        candidate = page.get_by_text(re.compile(r"^open$", re.I))
        count = await candidate.count()
        if count > 0:
            best_dy = float("inf")
            for i in range(count):
                b = await candidate.nth(i).bounding_box()
                if b:
                    dy = abs(b["y"] - box["y"])
                    if dy < best_dy:
                        best_dy, best_i = dy, i
            open_btn = candidate
            break
    if open_btn is None:
        return False
    await open_btn.nth(best_i).click()
    # The peek's own opening transition still intercepts clicks on its
    # content for a moment after this returns (confirmed: a property-value
    # click retried for the full 30s against "subtree intercepts pointer
    # events" before the transition had actually finished) — give it real
    # time to settle, not just a short fixed pause.
    await page.wait_for_timeout(2500)
    return True


async def _dex_row_click_targets(page: Page) -> list[dict]:
    """Table rows have no href at all (confirmed by direct inspection) —
    the only way to identify each row is by walking up from its 'Dex'
    Source pill until including one more ancestor level would start
    covering a second pill (i.e. a neighbouring row). Returns each row's
    Name-cell text plus its occurrence index among rows sharing that same
    text (raw mouse-coordinate clicks proved unreliable across page
    reloads — get_by_text(...).nth(occurrence) is the robust equivalent
    of clicking the same title again after a fresh navigation). Only
    currently-visible rows are covered, no scrolling is done."""
    rows = await page.evaluate(
        """() => {
            const isPill = (el) => el.textContent.trim() === 'Dex' && el.children.length === 0
                && !el.closest('[role="tab"]') && !el.closest('[role="tablist"]');
            const countPillsIn = (el) => Array.from(el.querySelectorAll('*')).filter(isPill).length;
            const pills = Array.from(document.querySelectorAll('*')).filter(isPill);
            const out = [];
            for (const pill of pills) {
                let node = pill;
                let rowEl = null;
                for (let i = 0; i < 15 && node.parentElement; i++) {
                    const parent = node.parentElement;
                    if (countPillsIn(parent) > 1) { rowEl = node; break; }
                    node = parent;
                }
                if (!rowEl) continue;
                const nameCell = Array.from(rowEl.querySelectorAll('*')).find(
                    el => el.children.length === 0 && el.textContent.trim().length > 0
                );
                if (!nameCell) continue;
                out.push(nameCell.textContent.trim());
            }
            return out;
        }"""
    )
    seen = {}
    targets = []
    for title in rows:
        occurrence = seen.get(title, 0)
        seen[title] = occurrence + 1
        targets.append({"title": title, "occurrence": occurrence})
    return targets


async def lister_elements_dex() -> list[dict]:
    page = await _get_page()
    await _open_item(page, DB1_CONTAINER_PAGE_ID)
    await _ensure_dex_view(page)

    # A direct click on a table row's title cell proved genuinely
    # unreliable (confirmed on both databases: sometimes it edits inline,
    # sometimes it navigates, no discernible pattern) — the hover-revealed
    # "Open" affordance is what actually works consistently, and needs no
    # search/indexing at all. Titles that are exact duplicates (only
    # possible with hand-crafted/test data — real usage titles are
    # expected to differ) will all resolve to whichever row DOM order
    # puts first; this is a known limitation, not a silent one.
    titles = [t["title"] for t in await _dex_row_click_targets(page) if t["title"] != SOURCE_VALUE]
    results = []
    for titre in titles:
        opened = await _open_row_via_hover(page, titre)
        if not opened:
            # A miss here is usually the previous row's peek-closing
            # animation not having fully finished yet — one retry after a
            # short pause resolves it rather than silently dropping the row.
            await page.wait_for_timeout(800)
            opened = await _open_row_via_hover(page, titre)
        if not opened:
            continue
        scope = await _active_scope(page)
        id_auto = await _read_id_auto(page, scope)
        status_val = await _get_property_value_text(page, scope, STATUS_PROPERTY)
        # Don't trust the view's own filter to keep Done rows out — this
        # tool's contract is "active rows only", enforced here regardless
        # of how the "Dex" view happens to be configured in Notion.
        if status_val != DONE_STATUS:
            results.append({
                "id": id_auto,
                "titre": titre,
                "status": status_val,
            })
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)
    return results


async def creer_element_db(titre: str, contenu: str, status: str) -> dict:
    if status not in ACTIVE_STATUSES:
        raise ValueError(
            f"Statut invalide: {status!r}. Autorises a la creation: {ACTIVE_STATUSES}"
        )
    page = await _get_page()
    await _open_item(page, DB1_CONTAINER_PAGE_ID)
    await _ensure_dex_view(page)

    new_page_btn = page.get_by_text("New page", exact=True)
    if await new_page_btn.count() == 0:
        new_page_btn = page.get_by_text("Nouvelle page", exact=True)
    await new_page_btn.first.click()
    await page.wait_for_timeout(1000)

    # "New page" always creates an inline table row with focus already
    # placed in its title field — type straight into it, no locator/click
    # needed at all (confirmed: trying to click a title-text locator here
    # sometimes matches the container page's own title instead).
    is_editable = await page.evaluate(
        "() => document.activeElement && "
        "document.activeElement.getAttribute('contenteditable') === 'true'"
    )
    if not is_editable:
        raise RuntimeError(
            "Focus inattendu apres 'New page' — pas de titre editable actif."
        )
    await page.keyboard.type(titre, delay=30)
    # Blur the still-open inline cell before hovering for "Open" — the row
    # needs to be in its normal (non-editing) state for the hover
    # affordance to appear.
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(500)

    opened = await _open_row_via_hover(page, titre)
    if not opened:
        raise RuntimeError(
            f"Impossible d'ouvrir la ligne '{titre}' juste apres sa creation "
            "(le bouton 'Open' au survol n'a pas ete trouve)."
        )

    scope = await _active_scope(page)
    await _set_select_property(page, scope, SOURCE_PROPERTY, SOURCE_VALUE)
    await _set_select_property(page, scope, STATUS_PROPERTY, status)
    await _set_body_content(page, scope, contenu, replace=False)
    id_auto = await _read_id_auto(page, scope)

    return {"id": id_auto, "titre": titre, "status": status}


async def modifier_element_db(
    id_ligne: str,
    nouveau_titre: Optional[str] = None,
    nouveau_contenu: Optional[str] = None,
    nouveau_status: Optional[str] = None,
) -> dict:
    if nouveau_status is not None and nouveau_status not in ACTIVE_STATUSES:
        raise ValueError(
            f"Statut invalide: {nouveau_status!r}. Autorises: {ACTIVE_STATUSES}"
        )
    page = await _get_page()
    await _open_by_id_auto(page, id_ligne)
    scope = await _active_scope(page)
    await _verify_source_is_dex(page, scope)

    if nouveau_titre is not None:
        await _set_title(page, scope, nouveau_titre)
    if nouveau_contenu is not None:
        await _set_body_content(page, scope, nouveau_contenu, replace=True)
    if nouveau_status is not None:
        await _set_select_property(page, scope, STATUS_PROPERTY, nouveau_status)

    return {"id": id_ligne, "modifie": True}


async def supprimer_element_db(id_ligne: str) -> dict:
    page = await _get_page()
    await _open_by_id_auto(page, id_ligne)
    scope = await _active_scope(page)
    await _verify_source_is_dex(page, scope)
    await _set_select_property(page, scope, STATUS_PROPERTY, DONE_STATUS)
    return {"id": id_ligne, "status": DONE_STATUS}


async def _db2_titles_for_date(page: Page, date_display: str) -> list[dict]:
    """DB2 table rows have no href either — same situation as DB1. Finds
    each row via the same ancestor-walk technique (climb from the exact
    date-text marker until one more level would cover a second row's
    date), then reads that row's Name cell as the title to search for.
    Starts from Playwright's own get_by_text locator (which properly
    waits for rendering) rather than a raw document.querySelectorAll('*')
    scan — that scan proved to sometimes run before the row was actually
    mounted, silently finding nothing even though the element was there
    moments later."""
    markers = page.get_by_text(date_display, exact=True)
    count = await markers.count()
    rows = []
    for i in range(count):
        row = await markers.nth(i).evaluate(
            """(marker, dateDisplay) => {
                // Confirmed by direct inspection: DB2's row-level ancestor
                // is the first one with more than one child (no intervening
                // multi-child wrapper like DB1's icon+text pair) — a marker
                // count check breaks here since only one row may be mounted
                // at a time (e.g. filtering to a single matching date).
                let node = marker;
                let rowEl = null;
                for (let i = 0; i < 15 && node.parentElement; i++) {
                    const parent = node.parentElement;
                    if (parent.children.length > 1) { rowEl = parent; break; }
                    node = parent;
                }
                if (!rowEl) return null;
                const isLeafText = (el) => !Array.from(el.children).some(c => c.textContent.trim().length > 0)
                    && el.textContent.trim().length > 0;
                const nameCell = Array.from(rowEl.querySelectorAll('*')).find(
                    el => isLeafText(el) && el.textContent.trim() !== dateDisplay
                );
                if (!nameCell) return null;
                const title = nameCell.textContent.trim();

                // Recurring meetings (e.g. a daily 1:1) reuse the exact
                // same title across many rows/dates — confirmed live,
                // querying two different dates returned the same meeting
                // because opening-by-title-alone always resolved to
                // whichever same-titled row came first. Record this row's
                // position among ALL same-titled rows in the main content
                // area (x > 300, same sidebar-exclusion boundary used
                // elsewhere) so it can be reopened precisely later,
                // instead of just by title.
                const sameTitled = Array.from(document.querySelectorAll('*')).filter(
                    el => isLeafText(el) && el.textContent.trim() === title
                        && el.getBoundingClientRect().x > 300
                );
                const occurrence = sameTitled.indexOf(nameCell);
                return {title, occurrence};
            }""",
            date_display,
        )
        if row:
            rows.append(row)
    return rows


async def lire_reunions_date(date: str) -> list[dict]:
    if not DATE_RE.match(date):
        raise ValueError(f"Date invalide: {date!r} — format attendu AAAA-MM-JJ.")

    page = await _get_page()
    await _open_item(page, DB2_CONTAINER_PAGE_ID)

    date_display = _notion_date_display(date)
    rows = await _db2_titles_for_date(page, date_display)

    results = []
    for row in rows:
        await _open_item(page, DB2_CONTAINER_PAGE_ID)
        opened = await _open_row_via_hover(page, row["title"], row["occurrence"])
        if not opened:
            continue
        # The hover-opened peek is layered on the table view — expand it to
        # a real full page (Ctrl+Enter) before reading, so property/tab
        # lookups run against the actual page, not the peek overlay.
        await page.keyboard.press("Control+Enter")
        await _settle(page)
        item = await _read_meeting_item(page)
        results.append(item)
    return results


async def _read_meeting_item(page: Page) -> dict:
    # Already navigated to the meeting's full page by the caller.
    id_auto = await _read_id_auto(page, page)

    nom = await page.locator("div.notion-page-block h1, [contenteditable] h1").first.inner_text()
    date_val = _clean_property_value(await _get_property_value_text(page, page, "Date"))
    participants = _clean_property_value(await _get_property_value_text(page, page, "Participants"))
    participants_ext = _clean_property_value(
        await _get_property_value_text(page, page, "Participants externes")
    )

    notes_md = await _read_tab_panel(page, "Notes")
    transcript_md = await _read_tab_panel(page, "Transcript")

    return {
        "id": id_auto,
        "nom": nom.strip(),
        "date": date_val,
        "participants": participants,
        "participants_externes": participants_ext,
        "notes_md": notes_md,
        "transcript_md": transcript_md,
    }


def _clean_property_value(value: str) -> str:
    """Notion shows the literal word 'Empty' as a placeholder for unset
    properties — that is not real content, normalize it away."""
    return "" if value.strip() == "Empty" else value.strip()


async def _read_tab_panel(page: Page, tab_name: str) -> str:
    """Click a Notes/Transcript tab and read only its own panel.

    role="tabpanel" is not unique on this page — confirmed live: the left
    sidebar has its own internal tabbed section (Meetings/Recents/...) and
    also carries role="tabpanel", and it sits earlier in the DOM than the
    AI Meeting Notes widget's real panel. An unscoped .first grabbed the
    sidebar's full text instead every time. The sidebar is confirmed to
    occupy roughly the left 300px (same boundary used for the "Recents"
    row-vs-sidebar collision in _open_row_via_hover) — pick the first
    tabpanel candidate found outside that zone instead."""
    tab = page.get_by_role("tab", name=tab_name, exact=True)
    if await tab.count() == 0:
        tab = page.get_by_text(tab_name, exact=True)
    await tab.first.click()
    await page.wait_for_timeout(500)

    candidates = page.get_by_role("tabpanel")
    count = await candidates.count()
    for i in range(count):
        box = await candidates.nth(i).bounding_box()
        if box and box["x"] > 300:
            return (await candidates.nth(i).inner_text()).strip()
    return (await page.locator("div.notion-page-content").inner_text()).strip()


# --- MCP wiring -----------------------------------------------------------

@app.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="lister_elements_dex",
            description="Lecture seule. Renvoie toutes les lignes actives (non terminees) "
                        "de la base 'Dex' (0 - To do).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="creer_element_db",
            description="Cree une nouvelle ligne dans la base Dex. Source='Dex' est force "
                        "automatiquement, non parametrable.",
            inputSchema={
                "type": "object",
                "properties": {
                    "titre": {"type": "string"},
                    "contenu": {"type": "string", "description": "Corps de la page (texte libre)"},
                    "status": {"type": "string", "enum": ACTIVE_STATUSES},
                },
                "required": ["titre", "contenu", "status"],
            },
        ),
        types.Tool(
            name="modifier_element_db",
            description="Modifie titre/contenu/statut d'une ligne deja creee par Dex "
                        "(Source='Dex' obligatoire, verifie avant toute ecriture).",
            inputSchema={
                "type": "object",
                "properties": {
                    "id_ligne": {"type": "string", "description": "Valeur de id_auto, ex. 'DEX-5'"},
                    "nouveau_titre": {"type": "string"},
                    "nouveau_contenu": {"type": "string"},
                    "nouveau_status": {"type": "string", "enum": ACTIVE_STATUSES},
                },
                "required": ["id_ligne"],
            },
        ),
        types.Tool(
            name="supprimer_element_db",
            description="'Supprime' une ligne Dex — en realite passe Status='Done', "
                        "jamais de suppression reelle. Non reversible dans cette version.",
            inputSchema={
                "type": "object",
                "properties": {"id_ligne": {"type": "string", "description": "Valeur de id_auto, ex. 'DEX-5'"}},
                "required": ["id_ligne"],
            },
        ),
        types.Tool(
            name="lire_reunions_date",
            description="Lecture seule. Renvoie les reunions (AI Meeting Notes) d'une date "
                        "exacte (AAAA-MM-JJ, jamais une plage), avec notes et transcript.",
            inputSchema={
                "type": "object",
                "properties": {"date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}},
                "required": ["date"],
            },
        ),
    ]


@app.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent]:
    arguments = arguments or {}
    async with _lock:
        try:
            if name == "lister_elements_dex":
                result = await lister_elements_dex()
            elif name == "creer_element_db":
                result = await creer_element_db(
                    arguments["titre"], arguments["contenu"], arguments["status"]
                )
            elif name == "modifier_element_db":
                result = await modifier_element_db(
                    arguments["id_ligne"],
                    arguments.get("nouveau_titre"),
                    arguments.get("nouveau_contenu"),
                    arguments.get("nouveau_status"),
                )
            elif name == "supprimer_element_db":
                result = await supprimer_element_db(arguments["id_ligne"])
            elif name == "lire_reunions_date":
                result = await lire_reunions_date(arguments["date"])
            else:
                return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            return [types.TextContent(type="text", text=f"Erreur: {exc}")]

    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


async def _main() -> None:
    logger.info("Starting Notion Guardrail MCP Server")
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="notion-guardrail-mcp",
                server_version="0.1.0",
                capabilities=app.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
