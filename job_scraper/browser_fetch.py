"""Headless-browser fallback for pages unavailable to plain HTTP clients."""

import logging
import threading


log = logging.getLogger("job_scraper")
_BROWSER_LIMIT = threading.BoundedSemaphore(2)
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def fetch_rendered_html(url, timeout_ms=60000, settle_ms=2500):
    with _CACHE_LOCK:
        if url in _CACHE:
            return _CACHE[url]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Browser rendering skipped: playwright is not installed")
        return None
    with _BROWSER_LIMIT:
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(channel="msedge", headless=True)
                except Exception:
                    browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(settle_ms)
                html = page.content() if response is not None and response.status < 400 else None
                browser.close()
            if html:
                with _CACHE_LOCK:
                    if len(_CACHE) >= 500:
                        _CACHE.pop(next(iter(_CACHE)))
                    _CACHE[url] = html
            return html
        except Exception as exc:
            log.warning("Browser rendering failed for %s: %s", url, str(exc)[:200])
            return None
