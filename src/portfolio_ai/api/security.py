"""Who may call the API, and what is kept about the visitors they call on behalf of.

**One caller.** The API is private to the Docker network and the only thing that
calls it is the portfolio's Express API, which attaches a bearer token server-side.
The browser never holds the token and never talks to this service at all -- which
is also why there is no CORS here: CORS is a rule browsers apply to pages, and no
page ever calls this.

**Visitors, second hand.** Since every request comes from the proxy, the visitor's
address and browser arrive as headers the proxy sets: ``X-Visitor-IP``,
``X-Visitor-User-Agent`` and ``X-Visitor-Referrer``. They are believed because only
the key holder can send them. The address is hashed the moment it arrives and never
exists anywhere else in this process.
"""

import hashlib
import hmac
import ipaddress
import secrets
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from portfolio_ai.config import Settings, get_settings
from portfolio_ai.db.chat import ClientInfo
from portfolio_ai.exceptions import ConfigError

# Browser strings run to a few hundred characters; anything past this is either a
# bug or someone seeing what happens.
MAX_USER_AGENT_CHARS = 512
MAX_REFERRER_CHARS = 512

# FastAPI's reader for "Authorization: Bearer <token>". auto_error=False, so a
# missing or malformed header arrives here as None and the 401 below is ours --
# with the WWW-Authenticate header the HTTP spec asks for. It also gives /docs an
# "Authorize" button.
bearer = HTTPBearer(auto_error=False, description="PORTFOLIO_AI_API_KEY")


def require_api_secrets(settings: Settings) -> None:
    """Refuse to serve without the two secrets the API cannot work safely without.

    Called at startup, so a missing key stops the process before it accepts a
    request. Without it the choice at request time would be between refusing every
    request -- a service that looks up and is useless -- and accepting every
    request, which is worse.
    """
    missing = [
        name
        for name, value in (
            ("PORTFOLIO_AI_API_KEY", settings.portfolio_ai_api_key),
            ("IP_HASH_SALT", settings.ip_hash_salt),
        )
        if value is None
    ]
    if missing:
        raise ConfigError(
            f"The API needs {' and '.join(missing)} set. Generate each with: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )


# async although nothing here awaits: FastAPI runs a plain `def` dependency in a
# worker thread, which is the right call for blocking code and pointless overhead
# for a comparison of two strings.
async def require_api_key(  # ruff: ignore[unused-async]
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    """Let the request through only with the right bearer token.

    ``secrets.compare_digest`` rather than ``==``. An ordinary comparison stops at
    the first character that differs, so how long it takes leaks how much of a guess
    was right -- measurable over enough requests. ``compare_digest`` takes the same
    time whatever the input.

    It compares bytes, not strings, deliberately: on a ``str`` it refuses anything
    outside ASCII with a TypeError, and header values are whatever the caller sent,
    so comparing strings would turn one odd header into a 500.
    """
    expected = get_settings().portfolio_ai_api_key
    if expected is None:
        # Startup refuses to run without a key (require_api_secrets), so this is a
        # process that skipped startup -- a test, or a bug. Fail closed.
        raise ConfigError("PORTFOLIO_AI_API_KEY is not set, so no request can be authenticated.")

    presented = credentials.credentials if credentials else ""
    if not credentials or not secrets.compare_digest(
        presented.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def hash_ip(address: str | None, key: SecretStr) -> str | None:
    """A keyed hash of an IP address, or None if there is no usable address.

    HMAC-SHA256 rather than a salted SHA-256: HMAC is the construction built for
    "hash this with a secret", and "salt plus hash" done by hand has known ways to
    go wrong. Keyed, the hash cannot be reversed by hashing all four billion IPv4
    addresses, which is an afternoon's work against a plain one.

    The address is parsed first and hashed in its standard written form, so the
    same visitor always gets the same hash:

    - ``::ffff:203.0.113.7`` is how Node reports an IPv4 visitor on a dual-stack
      socket. It is the same address as ``203.0.113.7`` and hashes the same.
    - Anything that does not parse is None rather than an error. The address is
      analytics, not identity; a proxy sending garbage should not cost a visitor
      their answer.
    """
    if not address:
        return None
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    return hmac.new(key.get_secret_value().encode(), str(ip).encode(), hashlib.sha256).hexdigest()


def _clip(value: str | None, limit: int) -> str | None:
    """Trimmed and cut to ``limit`` characters; None if nothing is left."""
    if value is None:
        return None
    return value.strip()[:limit] or None


def _page(referrer: str | None) -> str | None:
    """The page a conversation started on, without its query string or fragment.

    The page is the useful part ("people ask from the Threadline case study"). A
    query string is where tracking ids and, now and then, something personal live.
    """
    if not referrer:
        return None
    try:
        parts = urlsplit(referrer.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:MAX_REFERRER_CHARS]


async def visitor(  # ruff: ignore[unused-async] -- see require_api_key
    x_visitor_ip: Annotated[str | None, Header()] = None,
    x_visitor_user_agent: Annotated[str | None, Header()] = None,
    x_visitor_referrer: Annotated[str | None, Header()] = None,
) -> ClientInfo:
    """Where this request's visitor came from, as the proxy reported it.

    ``Header()`` reads a request header named after the parameter, with the
    underscores turned into hyphens: ``x_visitor_ip`` is ``X-Visitor-IP``.

    None of these can fail validation: each is optional and unconstrained, and bad
    values are cleaned up rather than refused. A proxy that sends none of them still
    gets its answer, with nothing recorded about where it came from. Missing analytics
    is a smaller failure than a visitor turned away over a header.
    """
    salt = get_settings().ip_hash_salt
    return ClientInfo(
        # No salt only happens in a process that skipped startup. Storing no hash is
        # the safe way to fail: nothing is recorded, rather than a raw address.
        ip_hash=hash_ip(x_visitor_ip, salt) if salt else None,
        user_agent=_clip(x_visitor_user_agent, MAX_USER_AGENT_CHARS),
        referrer=_page(x_visitor_referrer),
    )
