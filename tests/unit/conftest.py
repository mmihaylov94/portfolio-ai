"""Fixtures shared by every unit test.

Unit tests run with a fixed configuration and never read a ``.env`` file. That is
the difference between a test and a coincidence: a unit test that quietly reads the
developer's ``.env`` passes on the development machine and fails in CI, where there
is no such file -- and both results describe the machine rather than the code.

It happened. Embedding a search query reads the settings, the agent tests search,
and every one of them passed locally for the whole of step 3, then failed on the
first push with ``DATABASE_URL: Field required``.
"""

from collections.abc import Iterator

import pytest

from portfolio_ai.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The same settings for every unit test, on every machine.

    ``autouse`` so no test can forget it, which is the only way a rule like this
    holds. Integration tests are unaffected: this file only covers ``tests/unit``,
    and those tests need the real database named in the real configuration.
    """
    # Settings reads .env because its model_config says to, and model_config is an
    # ordinary dict on the class. Replacing the entry for the length of the test
    # switches the file off without touching it; monkeypatch puts it back after.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    # Nor anything else in the environment. Every setting can come from a variable
    # of the same name, and any of them may already be set: in a developer's shell,
    # or by the integration suite's fixture, which points DB_SCHEMA at its throwaway
    # schema for the whole session and leaked it into a unit test the first time both
    # suites ran in one process. `model_fields` lists every setting, so this cannot
    # fall behind when one is added.
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)

    # Enough to build a valid Settings. Nothing here is ever connected to: unit
    # tests have no database and no network. The URL only has to pass validation,
    # which locally means port 5433 -- see the port guard in config.py.
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unit:tests@localhost:5433/never-connected")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")

    # Settings are cached for the life of the process (lesson 3). Cleared on the way
    # in so this test sees the values above, and on the way out so the next one
    # cannot inherit whatever this one changed.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
