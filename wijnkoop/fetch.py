"""Polite page fetching: obeys robots.txt and waits between requests.

When a shop answers 429 Too Many Requests (or 503), WijnKoop waits as long as the shop's
Retry-After says (at least a minute), reads on at a slower pace, and after three refusals for the
same page raises ShopSaysWait, so the run pauses for hours instead of asking again and again.
"""
import logging
import time
import urllib.robotparser
from urllib.parse import urlsplit

import httpx

from .config import MIN_DELAY, USER_AGENT
from .pace import CALM_PAGES, SLOWEST, WAIT_IN_RUN, ShopSaysWait, retry_after

log = logging.getLogger("wijnkoop")


class NotAllowed(Exception):
    pass


class PoliteClient:
    def __init__(self, base_url: str, min_delay: float = MIN_DELAY):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9"},
            timeout=30,
            follow_redirects=True,
        )
        self.robots = urllib.robotparser.RobotFileParser()
        resp = self.client.get(self.base_url + "/robots.txt")
        if resp.status_code in (429, 503):
            raise ShopSaysWait(resp.status_code, retry_after(resp.headers.get("retry-after")))
        resp.raise_for_status()
        self.robots.parse(resp.text.splitlines())
        self.robots_delay = float(self.robots.crawl_delay(USER_AGENT) or 0)
        self.delay = max(min_delay, self.robots_delay)
        self.normal_delay = self.delay
        self._last = 0.0
        self.refusals = 0  # 429/503 answers in this run
        self.last_status = None
        self._calm = 0  # pages read since the last refusal

    def allowed(self, url: str) -> bool:
        return self.robots.can_fetch(USER_AGENT, url)

    def get(self, url: str, retries: int = 3) -> str:
        return self._fetch(url, retries).text

    def get_bytes(self, url: str, retries: int = 3) -> bytes:
        """For compressed sitemaps (.xml.gz)."""
        return self._fetch(url, retries).content

    def _fetch(self, url: str, retries: int = 3):
        if not self.allowed(url):
            raise NotAllowed(url)
        for attempt in range(retries):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.client.get(url)
            except httpx.TransportError:
                if attempt == retries - 1:
                    raise
                time.sleep(30 * (attempt + 1))
                continue
            if resp.status_code in (429, 503):
                self.slow_down(resp.status_code, resp.headers.get("retry-after"), attempt, retries)
                continue
            resp.raise_for_status()
            self.calm()
            return resp
        raise RuntimeError(f"Gave up on {url}")

    def slow_down(self, status: int, header: str | None, attempt: int, retries: int) -> None:
        """The shop asks us to slow down: wait, and read on at a slower pace. The third refusal for
        the same page (or a Retry-After of more than 15 minutes) pauses the run."""
        self.refusals += 1
        self.last_status = status
        self._calm = 0
        self.delay = min(SLOWEST, max(self.delay * 1.5, self.delay + 5))
        wait = retry_after(header)
        if attempt >= retries - 1 or (wait and wait > WAIT_IN_RUN):
            raise ShopSaysWait(status, wait)
        wait = max(wait or 0, 60 * (attempt + 1))
        log.warning("%s answers HTTP %s (too many requests): waiting %.0f s, then one page every %.0f s",
                    self.host(self.base_url), status, wait, self.delay)
        time.sleep(wait)

    def calm(self) -> None:
        """A page read without a refusal. After many of them the pace eases a little, never below normal."""
        self._calm += 1
        if self._calm >= CALM_PAGES and self.delay > self.normal_delay:
            self._calm = 0
            self.delay = max(self.normal_delay, self.delay * 0.9)

    @staticmethod
    def host(url: str) -> str:
        return urlsplit(url).netloc
