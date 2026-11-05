"""A small, polite HTTP client: per-host rate limit, retries with backoff, Retry-After, byte
counting, and a Range-request file view for reading only a zip's directory."""

from __future__ import annotations

import email.utils
import http.client
import json
import random
import re
import ssl
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

USER_AGENT = "agentprint-research/0.1 (security research)"
MAX_RATE = 5.0  # requests per second per host, hard ceiling
MAX_DECODED_BYTES = 256 * 1024 * 1024
MAX_RETRY_AFTER = 900.0  # longer waits are still honored, but the request is given up
REDIRECTS = {301, 302, 303, 307, 308}


class HttpError(Exception):
    """A request failed for good (after retries), or the reply made no sense."""


class TooLarge(Exception):
    """The reply is bigger than the caller allowed. Never retried."""


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lower-case names
    body: bytes
    wire_bytes: int  # body bytes as they came over the network (before gzip decoding)


@dataclass
class Tally:
    requests: int = 0
    bytes: int = 0

    def add(self, n: int) -> None:
        self.requests += 1
        self.bytes += n


def json_of(resp: Response, what: str) -> Any:
    if resp.status != 200:
        raise HttpError(f"HTTP {resp.status} for {what}")
    try:
        return json.loads(resp.body)
    except ValueError as exc:
        raise HttpError(f"bad JSON for {what}: {exc}") from exc


Transport = Callable[[str, str, dict[str, str], bytes | None, float, int | None], Response]


class RateLimiter:
    """At most `rate` request starts per second for each host, shared by all threads."""

    def __init__(
        self,
        default: float = 4.0,
        rates: dict[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rates = dict(rates or {})
        for rate in (default, *self.rates.values()):
            if not 0 < rate <= MAX_RATE:
                raise ValueError(f"rate must be in (0, {MAX_RATE}] requests/second, got {rate}")
        self.default, self.clock, self.sleep = default, clock, sleep
        self.next: dict[str, float] = {}
        self.lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self.lock:
            now = self.clock()
            start = max(now, self.next.get(host, now))
            self.next[host] = start + 1.0 / self.rates.get(host, self.default)
        if start > now:
            self.sleep(start - now)

    def pause(self, host: str, seconds: float) -> None:
        """Nobody starts a request to `host` for `seconds` (Retry-After, backoff)."""
        with self.lock:
            until = self.clock() + seconds
            self.next[host] = max(self.next.get(host, until), until)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # python.org builds on macOS ship without CA certificates; use the system bundle then.
    if ctx.cert_store_stats()["x509_ca"] == 0 and Path("/etc/ssl/cert.pem").is_file():
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


def default_transport() -> Transport:
    """http.client with one keep-alive connection per thread and host."""
    context = _ssl_context()
    local = threading.local()

    def transport(method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        conns = local.__dict__.setdefault("conns", {})
        key = (parts.scheme, parts.netloc)
        conn = conns.get(key)
        if conn is None:
            if parts.scheme == "https":
                conn = http.client.HTTPSConnection(parts.netloc, timeout=timeout, context=context)
            else:
                conn = http.client.HTTPConnection(parts.netloc, timeout=timeout)
            conns[key] = conn
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            length = resp.getheader("content-length")
            if max_bytes is not None and length and length.isdigit() and int(length) > max_bytes:
                raise TooLarge(f"{length} bytes > limit {max_bytes}")
            chunks, total = [], 0
            while chunk := resp.read(65536):
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise TooLarge(f"more than {max_bytes} bytes")
                chunks.append(chunk)
            if resp.will_close:
                conn.close()
                conns.pop(key, None)
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return Response(resp.status, hdrs, b"".join(chunks), total)
        except BaseException:
            conn.close()
            conns.pop(key, None)
            raise

    return transport


def _retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - now)


def _decode(resp: Response) -> bytes:
    if resp.headers.get("content-encoding", "").lower() != "gzip":
        return resp.body
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = inflater.decompress(resp.body, MAX_DECODED_BYTES)
    except zlib.error as exc:
        raise HttpError(f"bad gzip body: {exc}") from exc
    if inflater.unconsumed_tail:
        raise TooLarge(f"gzip body unpacks to more than {MAX_DECODED_BYTES} bytes")
    return data


class Http:
    def __init__(
        self,
        limiter: RateLimiter,
        transport: Transport | None = None,
        *,
        max_retries: int = 5,
        timeout: float = 30.0,
        backoff: float = 1.0,
        max_backoff: float = 300.0,
        jitter: Callable[[], float] = random.random,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.limiter = limiter
        self.transport = transport or default_transport()
        self.max_retries, self.timeout = max_retries, timeout
        self.backoff, self.max_backoff = backoff, max_backoff
        self.jitter, self.now = jitter, now
        self.total = Tally()
        self.hosts: dict[str, Tally] = {}
        self.lock = threading.Lock()

    def _count(self, host: str, n: int, tally: Tally | None) -> None:
        with self.lock:
            self.total.add(n)
            self.hosts.setdefault(host, Tally()).add(n)
        if tally is not None:
            tally.add(n)

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, body: bytes, **kw) -> Response:
        return self.request("POST", url, body=body, **kw)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        max_bytes: int | None = None,
        tally: Tally | None = None,
    ) -> Response:
        sent = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", **(headers or {})}
        for _redirect in range(6):
            resp = self._send(method, url, sent, body, max_bytes, tally)
            if resp.status in REDIRECTS and "location" in resp.headers:
                url = urljoin(url, resp.headers["location"])
                continue
            return Response(resp.status, resp.headers, _decode(resp), resp.wire_bytes)
        raise HttpError(f"too many redirects for {url}")

    def _send(self, method, url, headers, body, max_bytes, tally) -> Response:
        host = urlsplit(url).netloc
        problem = ""
        for attempt in range(self.max_retries + 1):
            self.limiter.wait(host)
            try:
                resp = self.transport(method, url, headers, body, self.timeout, max_bytes)
            except (OSError, http.client.HTTPException) as exc:
                problem, wait = f"{type(exc).__name__}: {exc}", None
            else:
                self._count(host, resp.wire_bytes, tally)
                if resp.status != 429 and resp.status < 500:
                    return resp
                problem = f"HTTP {resp.status}"
                wait = _retry_after(resp.headers.get("retry-after"), self.now())
            if wait is None:
                wait = min(self.backoff * 2**attempt, self.max_backoff) + self.jitter()
            self.limiter.pause(host, wait)  # every thread backs off from this host
            if wait > MAX_RETRY_AFTER:
                break
        raise HttpError(f"{problem} from {host} after {attempt + 1} tries")


_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


class RangeFile:
    """A read-only, seekable view of a remote file that downloads only the byte ranges read.

    zipfile reads the directory at the end of a zip, then each member it is asked for, so a
    wheel can be listed with one request and only wanted members are ever downloaded.
    """

    def __init__(
        self,
        http: Http,
        url: str,
        size: int,
        *,
        chunk: int = 64 * 1024,
        max_bytes: int = 64 * 1024 * 1024,
        tally: Tally | None = None,
    ) -> None:
        self.http, self.url, self.size = http, url, size
        self.chunk, self.max_bytes, self.tally = chunk, max_bytes, tally
        self.pos = 0
        self.fetched = 0
        self.spans: list[tuple[int, bytes]] = []
        if size <= chunk:
            self._fetch_whole()
        else:
            self._fetch(size - chunk, size)  # the zip directory lives at the end

    def _fetch_whole(self) -> None:
        resp = self.http.get(self.url, max_bytes=self.max_bytes, tally=self.tally)
        if resp.status != 200:
            raise HttpError(f"HTTP {resp.status} for {self.url}")
        self._whole(resp.body)

    def _whole(self, data: bytes) -> None:
        self.spans, self.size = [(0, data)], len(data)
        self.fetched += len(data)

    def _fetch(self, start: int, end: int) -> None:
        if self.fetched + (end - start) > self.max_bytes:
            raise TooLarge(f"would read more than {self.max_bytes} bytes of {self.url}")
        resp = self.http.get(
            self.url,
            headers={"Range": f"bytes={start}-{end - 1}", "Accept-Encoding": "identity"},
            max_bytes=self.max_bytes - self.fetched,
            tally=self.tally,
        )
        if resp.status == 200:  # the server ignored Range and sent everything
            self._whole(resp.body)
        elif resp.status == 416:
            self._fetch_whole()
        elif resp.status == 206:
            match = _CONTENT_RANGE.match(resp.headers.get("content-range", ""))
            if match is None:
                raise HttpError(f"206 without a usable Content-Range for {self.url}")
            self.spans.append((int(match.group(1)), resp.body))
            self.fetched += len(resp.body)
        else:
            raise HttpError(f"HTTP {resp.status} for {self.url}")

    def _cached(self, start: int, end: int) -> bytes | None:
        for begin, data in self.spans:
            if begin <= start and end <= begin + len(data):
                return data[start - begin : end - begin]
        return None

    def read(self, n: int | None = -1) -> bytes:
        end = self.size if n is None or n < 0 else min(self.size, self.pos + n)
        if end <= self.pos:
            return b""
        data = self._cached(self.pos, end)
        if data is None:
            self._fetch(self.pos, min(self.size, max(end, self.pos + self.chunk)))
            data = self._cached(self.pos, end)
            if data is None:
                raise HttpError(f"server sent a different byte range for {self.url}")
        self.pos = end
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.pos, 2: self.size}[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def tell(self) -> int:
        return self.pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self.spans = []
