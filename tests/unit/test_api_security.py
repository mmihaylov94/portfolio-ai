"""What is kept about a visitor: a keyed hash of the address, the page, the browser."""

from pydantic import SecretStr

from portfolio_ai.api.security import MAX_USER_AGENT_CHARS, _clip, _page, hash_ip

KEY = SecretStr("k" * 64)
OTHER_KEY = SecretStr("o" * 64)


def test_the_same_address_always_hashes_the_same() -> None:
    """Pinned to the digest itself, not only to itself twice. A change to how
    addresses are hashed would quietly stop every new hash matching the stored ones,
    and a returning visitor would look like a new one."""
    assert hash_ip("203.0.113.7", KEY) == (
        "3c95d91b5ea550cc3e126b0916a1a47682f26a85a5a343facc0738f4c61e2040"
    )


def test_the_hash_depends_on_the_key() -> None:
    """Without the key, hashing every IPv4 address would reverse it in an afternoon."""
    assert hash_ip("203.0.113.7", KEY) != hash_ip("203.0.113.7", OTHER_KEY)


def test_an_ipv4_visitor_on_a_dual_stack_socket_is_the_same_visitor() -> None:
    """Node reports IPv4 clients as ::ffff:a.b.c.d on a dual-stack socket."""
    assert hash_ip("::ffff:203.0.113.7", KEY) == hash_ip("203.0.113.7", KEY)


def test_ipv6_is_hashed_in_its_canonical_form() -> None:
    assert hash_ip("2001:DB8:0:0::1", KEY) == hash_ip("2001:db8::1", KEY)


def test_anything_that_is_not_an_address_is_not_hashed() -> None:
    assert hash_ip(None, KEY) is None
    assert hash_ip("", KEY) is None
    assert hash_ip("not-an-ip", KEY) is None
    assert hash_ip("203.0.113.7, 198.51.100.1", KEY) is None, "a forwarded list is not an address"


def test_the_referrer_keeps_the_page_and_drops_the_query() -> None:
    assert (
        _page("https://mihaylov.io/projects/threadline?utm_source=x&email=a@b.c#top")
        == "https://mihaylov.io/projects/threadline"
    )


def test_a_referrer_that_is_not_a_url_is_dropped() -> None:
    assert _page(None) is None
    assert _page("") is None
    assert _page("not a url") is None
    assert _page("/relative/path") is None


def test_a_user_agent_is_clipped_and_blank_is_nothing() -> None:
    long = "Mozilla/5.0 " + "x" * 2000

    clipped = _clip(long, MAX_USER_AGENT_CHARS)

    assert clipped is not None
    assert len(clipped) == MAX_USER_AGENT_CHARS
    assert _clip("   ", MAX_USER_AGENT_CHARS) is None
    assert _clip(None, MAX_USER_AGENT_CHARS) is None
