"""A stand-in for a module's logger, for tests of what a log line carries.

Importable from both suites because pyproject.toml puts ``tests/`` on pytest's
``pythonpath``, as it is for ``openai_fake.py``.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LogRecorder:
    """Stands in for a module's logger and keeps each line as a dict.

    Patched onto the module rather than captured through structlog, whose
    configuration depends on which tests happened to run before this one: a logger
    is cached the first time it is used, at whatever level was set then, and
    ``structlog.testing.capture_logs()`` does not reach one cached under an earlier
    configuration. A test of what is *not* logged that captured nothing would pass.
    """

    lines: list[dict[str, Any]] = field(default_factory=list)

    def _record(self, event: str, **fields: Any) -> None:
        self.lines.append({"event": event, **fields})

    debug = info = warning = error = _record

    def holds(self, text: str) -> bool:
        """Whether ``text`` appears anywhere in any line, in a name or a value."""
        return any(text in repr(line) for line in self.lines)
