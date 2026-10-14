"""Who the contact form may send an automatic reply to, and how many it may send a day.

``Replies`` by itself, with the clock handed in (``now=``), so that a day passes in a
line. The flow that asks it is tested in test_contact_service.py. The numbers are
written out, 24 hours and 10, so that a constant that moves fails here.
"""

import pytest

from portfolio_ai.contact import replies
from portfolio_ai.contact.replies import Refusal, Replies

HOUR = 3600.0
DAY = 24 * HOUR


@pytest.mark.parametrize(
    ("written", "inbox"),
    [
        ("someone@example.com", "someone@example.com"),
        ("SomeOne@Example.COM", "someone@example.com"),
        ("someone+news@example.com", "someone@example.com"),
        ("some.one@example.com", "some.one@example.com"),
        # An underscore, which Gmail does not allow, keeps these from being anybody's.
        ("no_such.person+x@gmail.com", "no_suchperson@gmail.com"),
        ("N.O_SUCH.P.E.R.S.O.N@googlemail.com", "no_suchperson@gmail.com"),
    ],
    ids=["as-it-is", "case", "a-plus-tag", "dots-elsewhere-count", "gmail-dots", "googlemail"],
)
def test_one_inbox_written_several_ways_is_one_inbox(written: str, inbox: str) -> None:
    """The usual ways of writing one inbox many times over, so that one reply a day
    means one a day to a person and not to a spelling. Gmail ignores the dots in a
    name; other providers may not, so theirs are kept."""
    assert replies.normalise(written) == inbox


def test_an_address_is_written_to_once_in_any_24_hours() -> None:
    count = Replies()

    assert count.take("someone@example.com", 10, now=0.0) is None
    # A minute short of a day, and written another way: the same inbox, refused.
    assert count.take("someone+again@example.com", 10, now=DAY - 60) == Refusal(
        "recipient", "This address was sent an automatic reply in the last 24 hours."
    )
    assert count.take("someone@example.com", 10, now=DAY) is None


def test_the_whole_service_sends_ten_a_day_and_no_more() -> None:
    count = Replies()

    for n in range(10):
        assert count.take(f"person{n}@example.com", 10, now=float(n)) is None
    assert count.take("person10@example.com", 10, now=10.0) == Refusal(
        "daily", "Today's automatic replies have all been sent (CONTACT_MAX_REPLIES_PER_DAY)."
    )
    # A day after the first of them, its place is free again, and one place only.
    assert count.take("person10@example.com", 10, now=DAY) is None
    assert count.take("person11@example.com", 10, now=DAY) is not None


def test_nought_a_day_sends_none() -> None:
    """The switch: CONTACT_MAX_REPLIES_PER_DAY=0 turns the replies off with no new release."""
    assert Replies().take("someone@example.com", 0, now=0.0) == Refusal(
        "daily", "Automatic replies are switched off (CONTACT_MAX_REPLIES_PER_DAY is 0)."
    )


def test_a_refusal_uses_up_nothing() -> None:
    count = Replies()

    assert count.take("someone@example.com", 2, now=0.0) is None
    assert count.take("someone@example.com", 2, now=1.0) is not None, "already written to"
    assert count.take("other@example.com", 2, now=2.0) is None, "so one place is still free"
    assert count.take("third@example.com", 2, now=3.0) is not None, "and now none is"


def test_what_is_a_day_old_is_forgotten() -> None:
    """Memory stays bounded by the day's cap, however long the process runs."""
    count = Replies()
    for n in range(5):
        count.take(f"person{n}@example.com", 10, now=float(n))

    count.take("later@example.com", 10, now=DAY + 10)

    held = vars(count)
    assert (len(held["_last"]), len(held["_sent"])) == (1, 1)


def test_no_address_is_held_as_itself() -> None:
    """A keyed hash, and a key of each process's own: two counts hold the same
    address as two different strings, neither of which holds any of it."""
    first, second = Replies(), Replies()
    first.take("someone@example.com", 10, now=0.0)
    second.take("someone@example.com", 10, now=0.0)

    [held] = vars(first)["_last"]
    assert "someone" not in held
    assert "example" not in held
    assert len(held) == 64, "the hex of a SHA-256 HMAC"
    assert list(vars(second)["_last"]) != [held]
