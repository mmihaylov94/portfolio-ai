"""Makes ``python -m portfolio_ai.assistant`` work.

Same shape as ``ingestion/__main__.py``: Python runs this file when a *package* is
given to ``-m``, so the CLI itself lives in ``cli.py`` and this only starts it.
"""

from portfolio_ai.assistant.cli import app

app()
