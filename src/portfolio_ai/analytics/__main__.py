"""Makes ``python -m portfolio_ai.analytics`` work, as the crontab runs it.

Same shape as ``ingestion/__main__.py``: the CLI lives in ``cli.py``.
"""

from portfolio_ai.analytics.cli import app

app()
