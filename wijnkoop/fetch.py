"""Polite page fetching: obeys robots.txt and waits between requests."""
import time
import urllib.robotparser
from urllib.parse import urlsplit

import httpx

from .config import MIN_DELAY, USER_AGENT


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
        resp.raise_for_status()
        self.robots.parse(resp.text.splitlines())
        crawl_delay = self.robots.crawl_delay(USER_AGENT) or 0
        self.delay = max(min_delay, float(crawl_delay))
        self._last = 0.0

    def allowed(self, url: str) -> bool:
        return self.robots.can_fetch(USER_AGENT, url)

    def get(self, url: str, retries: int = 3) -> str:
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
                # The shop asks us to slow down: back off and slow the whole run.
                self.delay = min(self.delay * 2, 120)
                time.sleep(60 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.text
        raise RuntimeError(f"Gave up on {url}")

    @staticmethod
    def host(url: str) -> str:
        return urlsplit(url).netloc
