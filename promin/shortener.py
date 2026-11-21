"""Short addresses for connecting a projector, made by a public URL shortener."""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Protocol

# What an alias is made of: typed with a TV remote, so lower case only and without the
# characters that are easy to mistake for each other (0 and o, 1 and l and i).
ALIAS_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
ALIAS_LENGTH = 6
ATTEMPTS = 3
TIMEOUT = 5.0


class ShortenerError(Exception):
    """The shortener gave no short address."""


class Shortener(Protocol):
    def shorten(self, url: str) -> str:
        """Returns the short address as it is typed, without the scheme. Blocks: call it
        from a thread."""


def new_alias() -> str:
    return "".join(secrets.choice(ALIAS_ALPHABET) for _ in range(ALIAS_LENGTH))


class IsGd:
    """is.gd: no account and no key. A link made there cannot be deleted or changed, so
    what ends a link is its token on this server, never the shortener."""

    endpoint = "https://is.gd/create.php"
    alias_taken = 2  # the errorcode for a problem with the alias we asked for

    def shorten(self, url: str) -> str:
        for _ in range(ATTEMPTS):
            answer = self._ask(url, new_alias())
            short = answer.get("shorturl")
            if isinstance(short, str) and short:
                return short.split("://", 1)[-1]
            if answer.get("errorcode") != self.alias_taken:
                raise ShortenerError(str(answer.get("errormessage") or answer))
        raise ShortenerError("no free alias in %d attempts" % ATTEMPTS)

    def _ask(self, url: str, alias: str) -> dict[str, object]:
        query = urllib.parse.urlencode({"format": "json", "url": url, "shorturl": alias})
        try:
            with urllib.request.urlopen("%s?%s" % (self.endpoint, query), timeout=TIMEOUT) as r:
                body = r.read(4096)
        except urllib.error.HTTPError as exc:
            # An error may come with an HTTP error status and the explanation in the body.
            body = exc.read(4096)
        except OSError as exc:
            raise ShortenerError(str(exc)) from exc
        try:
            answer = json.loads(body)
        except ValueError as exc:
            raise ShortenerError("not JSON: %r" % body[:100]) from exc
        if not isinstance(answer, dict):
            raise ShortenerError("unexpected answer: %r" % body[:100])
        return answer


SHORTENERS = {"isgd": IsGd}


def create(name: str) -> Optional[Shortener]:
    """The shortener the settings name, or None when links are switched off."""
    if not name:
        return None
    if name not in SHORTENERS:
        raise RuntimeError(
            "PROMIN_SHORTENER: unknown %r, expected one of: %s" % (name, ", ".join(SHORTENERS))
        )
    return SHORTENERS[name]()
