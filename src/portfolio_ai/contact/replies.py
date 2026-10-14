"""Who the contact form may send an automatic reply to, and how many it may send.

The form takes any address. Anybody can type somebody else's, and have this service
send that person an email from "Mihail Mihaylov". The reply is written by a model and
checked like input, so what it can say is bounded (``reply.fault``). How often it can
be sent is bounded here, by two limits:

- **One reply per address in any 24 hours.** So nobody can be written to over and
  over. The address is normalised first, so that the usual ways of writing one inbox
  several ways count once (:func:`normalise`).
- **So many replies in any 24 hours for the whole service**
  (``CONTACT_MAX_REPLIES_PER_DAY``, 10 by default). So the worst a determined sender
  can do is that many unwanted emails a day, however many addresses they type. 0
  switches the replies off.

A message refused a reply still reaches the owner, whose notification says why. The
likeliest harm from such mail is not the mail itself but spam complaints against the
sending account, which is also the account the owner's own notifications come from.

**Kept in memory, and forgotten on a restart.** One process, so that is enough. A
restart is a deploy, a change of setting, a crash or a reboot of the server, none
of which a sender outside can bring about on purpose. Each address is held as a keyed
hash and never as the address, with a key made when the process starts: the state
lives no longer than the process, so a key that lives as long loses nothing.
"""

import hashlib
import hmac
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

DAY_SECONDS = 24 * 3600.0

# The two names Gmail answers to, where the dots in the name before the "@" are not
# part of it: no_such.person@gmail.com is no_suchperson@gmail.com. (An underscore,
# which Gmail does not allow, keeps that from being anybody's real address.)
_GMAIL = frozenset({"gmail.com", "googlemail.com"})

# What the owner's notification says when a reply is not sent. Fixed wording, so it
# may be logged and shown.
RECENT = "This address was sent an automatic reply in the last 24 hours."
ALL_SENT = "Today's automatic replies have all been sent (CONTACT_MAX_REPLIES_PER_DAY)."
SWITCHED_OFF = "Automatic replies are switched off (CONTACT_MAX_REPLIES_PER_DAY is 0)."


@dataclass(frozen=True)
class Refusal:
    """Why a reply may not be sent: which limit, and the sentence for the owner."""

    limit: Literal["recipient", "daily"]
    why: str


def normalise(address: str) -> str:
    """One inbox written one way.

    In lower case, with everything from a ``+`` onwards dropped from the name, which
    most providers deliver to the same inbox ("plus addressing"), and for Gmail
    without the dots it ignores. Two real people at a provider that does neither
    could share a limit as a result; the cost of that is one fewer reply.
    """
    name, _, domain = address.lower().rpartition("@")
    name = name.partition("+")[0]
    if domain in _GMAIL:
        name, domain = name.replace(".", ""), "gmail.com"
    return f"{name}@{domain}"


class Replies:
    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
        # The last reply to each recipient, by keyed hash.
        self._last: dict[str, float] = {}
        # When each reply of the last 24 hours was let through, oldest first.
        self._sent: deque[float] = deque()

    def _who(self, address: str) -> str:
        """The address as this object holds it: an HMAC, which cannot be read back."""
        digest = hmac.new(self._key, normalise(address).encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()

    def _forget(self, moment: float) -> None:
        """Drop what is more than a day old, so that memory stays bounded by the cap."""
        while self._sent and moment - self._sent[0] >= DAY_SECONDS:
            self._sent.popleft()
        self._last = {who: when for who, when in self._last.items() if moment - when < DAY_SECONDS}

    def take(self, address: str, daily_limit: int, *, now: float | None = None) -> Refusal | None:
        """Let one reply to ``address`` through and count it, or say why not.

        None when the reply may be sent, in which case it is counted at once. Nothing
        is awaited between the checks and the counting, so two messages handled at
        the same moment cannot both slip through: asyncio switches between tasks only
        at an ``await``. ``time.monotonic``, which only goes forwards, measures the
        day; ``now`` is there for the tests.
        """
        moment = time.monotonic() if now is None else now
        self._forget(moment)
        if daily_limit == 0:
            return Refusal("daily", SWITCHED_OFF)
        who = self._who(address)
        if who in self._last:
            return Refusal("recipient", RECENT)
        if len(self._sent) >= daily_limit:
            return Refusal("daily", ALL_SENT)
        self._last[who] = moment
        self._sent.append(moment)
        return None
