#!/usr/bin/env python3
"""Smoke the rendered UI over HTTP with a real browser and stable hooks.

Usage:
  python3 scripts/ui_smoke.py --base-url http://127.0.0.1:8094 \
      --password-file data/.initial-admin-password [--username admin]

Requires Playwright and a Chromium binary (CHROMIUM env, default /usr/bin/chromium).
The run signs in through the normal form, keeps JavaScript disabled, and checks
that every screen still exposes the region ids documented in docs/frontend.md.
"""

import argparse
import os
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

SCREENS = {
    "/": ("open-work", "awaiting-pickup"),
    "/repairs": ("repair-search-title", "repair-results"),
    "/repairs/1": ("status", "device-details", "log", "stage-form", "pickup-state", "payment-state", "payment-total", "fee-labor", "stock-usage", "manual-part-form", "activity-heading"),
    "/customers": ("customer-search-title", "customer-results"),
    "/customers/1": ("customer-record", "customer-repairs"),
    "/repairs/new": ("customer-q", "intake-customers"),
    "/repairs/new?customer_id=1": ("repair-form-customer", "repair-form-device", "repair-form-problem"),
    "/customers/new": ("customer-form-details",),
    "/inventory": ("inventory-search-title", "inventory-results"),
    "/inventory/new": ("inventory-form-details",),
    "/users": ("user-results",),
    "/users/new": ("user-form-details",),
    "/settings": ("shop-profile", "file-limits"),
    "/account": ("account-profile", "account-language"),
    "/account/tokens": ("token-form", "active-tokens"),
    "/system": ("main",),
}

RECORD_LANES = {
    "/customers/1": ("customer-record", "customer-repairs", "customer-history"),
    "/repairs/1": ("status", "log"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8094")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1280)
    args = parser.parse_args()
    password = args.password_file.read_text(encoding="utf-8").strip()
    failures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM", "/usr/bin/chromium"))
        page = browser.new_page(viewport={"width": args.width, "height": 900},
                                java_script_enabled=False)
        page.goto(f"{args.base_url}/login")
        page.fill("#username", args.username)
        page.fill("#password", password)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(f"{args.base_url}/")
        for path, hooks in SCREENS.items():
            response = page.goto(args.base_url + path)
            if response is None or response.status != 200:
                failures.append(f"{path}: HTTP {getattr(response, 'status', 'none')}")
                continue
            missing = [hook for hook in hooks if page.locator(f"#{hook}").count() == 0]
            if missing:
                failures.append(f"{path}: missing {', '.join(missing)}")
            if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                failures.append(f"{path}: horizontal page overflow")
            if path in RECORD_LANES:
                regions = [page.locator(f'section[aria-labelledby="{hook}"]').first
                           for hook in RECORD_LANES[path]]
                if not all(region.count() for region in regions):
                    failures.append(f"{path}: missing record lane regions")
                    continue
                boxes = [region.bounding_box() for region in regions]
                if not all(boxes) or boxes[1]["y"] <= boxes[0]["y"]:
                    failures.append(f"{path}: record identity must precede the work")
                elif len(boxes) == 3 and (
                    boxes[2]["y"] < boxes[1]["y"] + boxes[1]["height"]
                    or abs(boxes[1]["width"] - boxes[2]["width"]) > 1
                ):
                    failures.append(f"{path}: work and support must stack")
                if path == "/repairs/1":
                    log_box = page.locator('section[aria-labelledby="log"]').first.bounding_box()
                    parts_box = page.locator('section[aria-labelledby="stock-usage"]').first.bounding_box()
                    if not log_box or not parts_box:
                        failures.append(f"{path}: work log or parts used missing")
                    elif args.width >= 1024:
                        if parts_box["x"] < log_box["x"] + log_box["width"] or abs(log_box["y"] - parts_box["y"]) > 1:
                            failures.append(f"{path}: quick parts must sit beside the work log")
                    elif parts_box["y"] > log_box["y"]:
                        failures.append(f"{path}: quick parts must come before the work log on phones")
                    activity = page.locator('section[aria-labelledby="activity-heading"] details')
                    trigger = activity.locator('summary.obi-panel-bar')
                    if activity.get_attribute("open") is not None:
                        failures.append(f"{path}: activity should start closed")
                    trigger.press("Enter")
                    if activity.get_attribute("open") is None:
                        failures.append(f"{path}: activity did not open from its title bar")
                    trigger.press("Enter")
                    if activity.get_attribute("open") is not None:
                        failures.append(f"{path}: activity did not close from its title bar")
                    if not page.locator("#stage-form").is_visible():
                        failures.append(f"{path}: status editor should be visible")
                    for panel, control in (("log", "#entry-body"),):
                        window = page.locator(f'section[aria-labelledby="{panel}"]')
                        editor = window.locator(control).first
                        trigger = window.locator("details > summary.obi-panel-bar")
                        if not editor.is_visible() or not trigger.is_visible():
                            failures.append(f"{path}: {panel} should start open")
                            continue
                        trigger.focus()
                        page.keyboard.press("Enter")
                        if editor.is_visible():
                            failures.append(f"{path}: {panel} did not fold by keyboard")
                        page.keyboard.press("Enter")
                        if not editor.is_visible():
                            failures.append(f"{path}: {panel} did not reopen by keyboard")
        browser.close()
    if failures:
        print("ui_smoke: FAIL")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"ui_smoke: {len(SCREENS)} screens passed at {args.width}px without JavaScript")
    return 0


if __name__ == "__main__":
    sys.exit(main())
