"""Makes ``python -m portfolio_ai.ingestion`` work.

Python runs this file when a *package* is given to ``-m``, which is why it exists
as a separate file rather than living in ``cli.py``. Production invokes it this way
from ``docker/crontab``.
"""

from portfolio_ai.ingestion.cli import app

app()
