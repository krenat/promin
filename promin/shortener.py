"""Short addresses for connecting a projector, made by a public URL shortener."""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Protocol, Tuple

# What an alias is made of: typed with a TV remote, so lower case only and without the
# characters that are easy to mistake for each other (0 and o, 1 and l and i).
ALIAS_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
ALIAS_LENGTH = 5
ATTEMPTS = 3
TIMEOUT = 5.0

log = logging.getLogger(__name__)


class ShortenerError(Exception):
    """The shortener gave no short address."""


class Shortener(Protocol):
    def shorten(self, url: str, ttl: int) -> str:
        """Returns the short address as it is typed, without the scheme; it is to stop
        working after `ttl` seconds. Blocks: call it from a thread."""


def new_alias() -> str:
    return "".join(secrets.choice(ALIAS_ALPHABET) for _ in range(ALIAS_LENGTH))


def named(url: str) -> str:
    """The same address with a name in place of a bare IPv4 address: 1.2.3.4 becomes
    1.2.3.4.nip.io, which a public DNS service resolves back to 1.2.3.4 without anything
    being registered. An address that has a name already is returned as it is."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        if ipaddress.ip_address(host).version != 4:
            return url
    except ValueError:
        return url
    netloc = "%s.nip.io" % host + (":%d" % parts.port if parts.port else "")
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


class SpooMe:
    """spoo.me: no account and no key, and a link can be told when to expire. Without an
    account a link cannot be deleted, so every link is made to expire by itself: nothing
    that names this server stays at the shortener.

    It refuses an address with a bare IP in it (checked 21 November 2026), so a server
    without a name of its own is given one that needs no registration: see `named`."""

    endpoint = "https://spoo.me/api/v1/shorten"
    host = "spoo.me"

    def shorten(self, url: str, ttl: int) -> str:
        expires = int(time.time()) + ttl
        url = named(url)
        for _ in range(ATTEMPTS):
            alias = new_alias()
            status, answer = self._ask({"long_url": url, "alias": alias, "expire_after": expires})
            if status in (200, 201):
                short = answer.get("short_url")
                if isinstance(short, str) and short:
                    return short.split("://", 1)[-1]
                return "%s/%s" % (self.host, alias)
            if not self._alias_taken(status, answer):
                raise ShortenerError("HTTP %d: %s" % (status, answer.get("error") or answer))
        raise ShortenerError("no free alias in %d attempts" % ATTEMPTS)

    @staticmethod
    def _alias_taken(status: int, answer: Dict[str, object]) -> bool:
        return status == 409 or answer.get("field") == "alias"

    def _ask(self, fields: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(fields).encode("utf-8"), method="POST",
            headers={
                "Content-Type": "application/json", "Accept": "application/json",
                # Some sites refuse the name urllib gives itself.
                "User-Agent": "Mozilla/5.0 (compatible; short-link-client)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
                status, body = r.status, r.read(4096)
        except urllib.error.HTTPError as exc:
            # A refusal comes with an HTTP error status and the explanation in the body.
            status = exc.code
            try:
                body = exc.read(4096)
            except Exception:
                # An error without a body fails to be read in a way that depends on the
                # Python version; whatever it is, there is no answer.
                raise ShortenerError("HTTP %d" % exc.code) from exc
        except (OSError, http.client.HTTPException) as exc:
            # No network, a timeout, or an answer cut short.
            raise ShortenerError(repr(exc)) from exc
        try:
            answer = json.loads(body)
        except ValueError as exc:
            raise ShortenerError("HTTP %d, not JSON: %r" % (status, body[:100])) from exc
        if not isinstance(answer, dict):
            raise ShortenerError("HTTP %d, unexpected answer: %r" % (status, body[:100]))
        return status, answer


SHORTENERS = {"spoo": SpooMe}


def create(name: str) -> Optional[Shortener]:
    """The shortener the settings name, or None when links are switched off. A name that
    is not known switches them off too: a wrong value must not stop the service."""
    if not name:
        return None
    if name not in SHORTENERS:
        log.warning(
            "Ignoring PROMIN_SHORTENER=%r: expected one of: %s", name, ", ".join(SHORTENERS)
        )
        return None
    return SHORTENERS[name]()
