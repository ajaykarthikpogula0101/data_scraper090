import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import DEFAULT_TIMEOUT, REQUEST_HEADERS


class ScrapeSession:
    """Thread-safe HTTP session factory with retries."""

    def __init__(self, timeout=DEFAULT_TIMEOUT, max_retries=2):
        self.timeout = timeout
        self.max_retries = max_retries
        self._local = threading.local()

    def _get_session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            retry = Retry(
                total=self.max_retries,
                connect=self.max_retries,
                read=self.max_retries,
                status=self.max_retries,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "HEAD"]),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(
                max_retries=retry, pool_connections=8, pool_maxsize=16
            )
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            s.headers.update(REQUEST_HEADERS)
            self._local.session = s
        return s

    def fetch(self, url, timeout=None, method="GET", **kwargs):
        """GET (or POST) a URL and return the response, or None on failure."""
        try:
            s = self._get_session()
            if method.upper() == "POST":
                return s.post(url, timeout=timeout or self.timeout, **kwargs)
            return s.get(url, timeout=timeout or self.timeout, **kwargs)
        except requests.exceptions.RequestException:
            return None

    def fetch_text(self, url, timeout=None, **kwargs):
        r = self.fetch(url, timeout=timeout, **kwargs)
        if r is None:
            return None
        if r.status_code not in (200, 201, 203):
            return None
        ctype = r.headers.get("Content-Type", "").lower()
        if "json" in ctype:
            return r.text
        return r.text

    def fetch_json(self, url, timeout=None, **kwargs):
        r = self.fetch(url, timeout=timeout, **kwargs)
        if r is None:
            return None
        try:
            return r.json()
        except Exception:
            return None

    def post_json(self, url, payload, timeout=None):
        r = self.fetch(url, timeout=timeout, method="POST", json=payload)
        if r is None:
            return None
        try:
            return r.json()
        except Exception:
            return None

    def head_ok(self, url, timeout=8):
        r = self.fetch(url, timeout=timeout, method="HEAD", allow_redirects=True)
        if r is None:
            return False
        return r.status_code < 400
