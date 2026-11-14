"""Reading shops whose prices only appear after the page's own JavaScript has run.

BrowserClient opens a page the way a visitor's browser does (headless Chromium via
Playwright), waits until it is quiet and returns the finished HTML. It obeys the same rules
as PoliteClient: robots.txt, its crawl-delay (at least MIN_DELAY), the honest user agent,
and slowing down when the shop asks. Only the shop's own site is loaded: no images, video,
fonts, or third-party trackers.
Sitemaps and robots.txt are plain files and still go through httpx.
"""
import os
import time
from urllib.parse import urlsplit

from .config import USER_AGENT
from .fetch import NotAllowed, PoliteClient

SKIP = {"image", "media", "font"}


class BrowserClient(PoliteClient):
    def __init__(self, base_url: str, **kw):
        super().__init__(base_url, **kw)
        self._pw = self._browser = self._page = None

    def _open(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            executable_path=os.environ.get("WIJNKOOP_CHROMIUM") or None,
            args=["--disable-dev-shm-usage", "--no-sandbox"])
        ctx = self._browser.new_context(user_agent=USER_AGENT, locale="nl-NL")
        # Only the shop's own site: no images, fonts, trackers, ads or cookie banners.
        site = ".".join(urlsplit(self.base_url).hostname.split(".")[-2:])

        def keep(route):
            host = urlsplit(route.request.url).hostname or ""
            own = host == site or host.endswith("." + site)
            if own and route.request.resource_type not in SKIP:
                route.continue_()
            else:
                route.abort()

        ctx.route("**/*", keep)
        self._page = ctx.new_page()

    def render(self, url: str, retries: int = 3) -> str:
        if not self.allowed(url):
            raise NotAllowed(url)
        if self._page is None:
            self._open()
        for attempt in range(retries):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self._page.goto(url, wait_until="networkidle", timeout=90_000)
            except Exception:
                if attempt == retries - 1:
                    raise
                time.sleep(30 * (attempt + 1))
                continue
            if resp is not None and resp.status in (429, 503):
                self.slow_down(resp.status, (resp.headers or {}).get("retry-after"), attempt, retries)
                continue
            if resp is not None and resp.status >= 400:
                raise RuntimeError(f"{resp.status} for {url}")
            self.calm()
            return self._page.content()
        raise RuntimeError(f"Gave up on {url}")

    def get(self, url: str, retries: int = 3) -> str:
        """XML files (sitemaps) as plain text; product pages rendered."""
        if url.endswith(".xml") or url.endswith(".txt"):
            return super().get(url, retries)
        return self.render(url, retries)

    def close(self):
        for thing in (self._browser, self._pw):
            try:
                if thing is self._pw and thing:
                    thing.stop()
                elif thing:
                    thing.close()
            except Exception:
                pass
        self._pw = self._browser = self._page = None
