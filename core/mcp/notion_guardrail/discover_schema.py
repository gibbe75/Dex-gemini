#!/usr/bin/env python3
"""One-off diagnostic script — not part of the delivered MCP server.

Opens Page A, and if it lands on the login screen, waits (without
closing the window) for the human to log in by hand using the "E-mail"
option (not Google) directly inside the visible window. Once the URL
moves away from /login, takes a screenshot and text dump.
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "browser_profile"
OUT_DIR = HERE / "schema_discovery"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PAGE_A_ID = "246d278a-b26e-8168-87d0-000b2cf6bfb7"


def snapshot(page, tag: str) -> None:
    try:
        print(f"[{tag}] url={page.url} title={page.title()!r}", flush=True)
    except Exception as exc:
        print(f"[{tag}] etat illisible: {exc}", flush=True)
        return
    try:
        page.screenshot(path=str(OUT_DIR / f"{tag}.png"), timeout=8000)
    except Exception as exc:
        print(f"[{tag}] capture ratee: {exc}", flush=True)


def main() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            channel="chrome",
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        try:
            page.goto(
                f"https://www.notion.so/{PAGE_A_ID.replace('-', '')}",
                timeout=30000,
            )
        except Exception as exc:
            print(f"goto error immediat: {exc}", flush=True)

        time.sleep(3)
        snapshot(page, "start")

        print("Connecte-toi via 'E-mail' (pas Google) dans la fenetre "
              "ouverte, pour de vrai cette fois. Attente fixe de 240s, "
              "pas de sortie anticipee...", flush=True)
        deadline = time.time() + 240
        last_url = None
        while time.time() < deadline:
            time.sleep(5)
            url = page.url
            if url != last_url:
                print(f"  ... url={url}", flush=True)
                last_url = url
        print("Attente terminee.", flush=True)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        snapshot(page, "final")

        text_path = OUT_DIR / "final.txt"
        try:
            text_path.write_text(
                page.locator("body").inner_text(timeout=10000), encoding="utf-8"
            )
        except Exception as exc:
            print(f"extraction texte ratee: {exc}", flush=True)

        print(f"URL finale: {page.url}", flush=True)
        context.close()
    print("Termine.")


if __name__ == "__main__":
    main()
