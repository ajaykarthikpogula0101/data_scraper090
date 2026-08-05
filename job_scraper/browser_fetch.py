"""Persistent, Windows-safe browser rendering workers with disk caching."""

import atexit
import asyncio
import hashlib
import logging
import os
import queue
import re
import threading
import time

from .config import BASE_DIR


log = logging.getLogger("job_scraper")
_CACHE_DIR = os.path.join(BASE_DIR, ".browser_cache")
_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_MEMORY_CACHE = {}
_CACHE_LOCK = threading.Lock()
_WORKERS = []
_WORKERS_LOCK = threading.Lock()
_NEXT_WORKER = 0


def _cache_path(url):
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(_CACHE_DIR, digest + ".html")


def _read_cache(url):
    with _CACHE_LOCK:
        if url in _MEMORY_CACHE:
            return _MEMORY_CACHE[url]
    path = _cache_path(url)
    try:
        if time.time() - os.path.getmtime(path) > _CACHE_TTL_SECONDS:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            html = handle.read()
        with _CACHE_LOCK:
            _MEMORY_CACHE[url] = html
        return html
    except (OSError, UnicodeError):
        return None


def _write_cache(url, html):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = _cache_path(url)
        temporary = path + ".tmp-%s" % threading.get_ident()
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            handle.write(html)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        with _CACHE_LOCK:
            if len(_MEMORY_CACHE) >= 500:
                _MEMORY_CACHE.pop(next(iter(_MEMORY_CACHE)))
            _MEMORY_CACHE[url] = html
    except OSError as exc:
        log.warning("Could not cache rendered page %s: %s", url, str(exc)[:150])


class _RenderRequest:
    def __init__(self, url, timeout_ms, settle_ms):
        self.url = url
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.html = None
        self.error = None
        self.done = threading.Event()


class _BrowserWorker:
    def __init__(self, number):
        self.number = number
        self.requests = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="browser-render-%d" % number,
                                       daemon=True)
        self.thread.start()

    def submit(self, request):
        self.requests.put(request)

    def stop(self):
        self.requests.put(None)

    def _run(self):
        if os.name == "nt" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self._fail_pending("playwright is not installed: %s" % exc)
            return
        playwright = browser = context = None
        try:
            playwright = sync_playwright().start()
            try:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
            except Exception:
                browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            while True:
                request = self.requests.get()
                if request is None:
                    break
                page = None
                try:
                    page = context.new_page()
                    response = page.goto(request.url, wait_until="domcontentloaded",
                                         timeout=request.timeout_ms)
                    page.wait_for_timeout(request.settle_ms)
                    more_pattern = re.compile(
                        r"^(load|show|view|see|indlæs|vis)\s+(more|flere)(?:\s+(jobs?|positions?|openings?))?$",
                        re.I,
                    )
                    for _ in range(10):
                        button = page.get_by_role("button", name=more_pattern).first
                        if button.count() == 0 or not button.is_visible() or not button.is_enabled():
                            break
                        before = len(page.content())
                        button.click(timeout=5000)
                        page.wait_for_timeout(800)
                        if len(page.content()) <= before:
                            break
                    if response is not None and response.status < 400:
                        request.html = page.content()
                except Exception as exc:
                    request.error = exc
                finally:
                    if page:
                        try:
                            page.close()
                        except Exception:
                            pass
                    request.done.set()
        except Exception as exc:
            log.warning("Browser worker %d failed: %s", self.number, str(exc)[:200])
            self._fail_pending(str(exc))
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def _fail_pending(self, message):
        while True:
            try:
                request = self.requests.get_nowait()
            except queue.Empty:
                return
            if request is not None:
                request.error = RuntimeError(message)
                request.done.set()


def _ensure_workers():
    global _WORKERS
    with _WORKERS_LOCK:
        if not _WORKERS:
            _WORKERS = [_BrowserWorker(1), _BrowserWorker(2)]
    return _WORKERS


def fetch_rendered_html(url, timeout_ms=60000, settle_ms=2500):
    global _NEXT_WORKER
    cached = _read_cache(url)
    if cached is not None:
        return cached
    workers = _ensure_workers()
    with _WORKERS_LOCK:
        worker = workers[_NEXT_WORKER % len(workers)]
        _NEXT_WORKER += 1
    request = _RenderRequest(url, timeout_ms, settle_ms)
    worker.submit(request)
    if not request.done.wait((timeout_ms + settle_ms + 15000) / 1000.0):
        log.warning("Browser rendering timed out for %s", url)
        return None
    if request.error:
        log.warning("Browser rendering failed for %s: %s", url, str(request.error)[:200])
        return None
    if request.html:
        _write_cache(url, request.html)
    return request.html


def close_browser_workers():
    with _WORKERS_LOCK:
        for worker in _WORKERS:
            worker.stop()


atexit.register(close_browser_workers)
