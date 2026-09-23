"""The HTTP API: Rachel over HTTP, for the portfolio site's Express API to call.

Private to the Docker network, authenticated with a bearer token, limited per
conversation and in total spend, streamed as server-sent events. ``main.py``
assembles it; ``docs/API.md`` walks through it module by module.
"""
