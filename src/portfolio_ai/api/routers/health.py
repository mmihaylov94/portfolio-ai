"""``/healthz`` and ``/readyz``: is the process up, and can it do its job.

Two questions with different answers. A process whose database is briefly
unreachable is alive -- restarting it would not help -- but it is not ready.
Docker's health check asks the first; the deploy script asks the second, once,
after starting a new version.

No authentication on either. They say nothing a caller could use, and the Docker
health check runs inside the container without the key.
"""

from fastapi import APIRouter, Response, status

from portfolio_ai.db.pool import health_check

router = APIRouter(tags=["health"])


# async with nothing to await: a plain `def` endpoint would be sent to a worker
# thread for no reason, which for an endpoint called every thirty seconds is waste.
@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """The process is running and answering HTTP. Deliberately checks nothing else."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response) -> dict[str, str]:
    """The database answers. 503 if not.

    ``response: Response`` is FastAPI handing over the response it is about to send,
    so the status can be changed while the return value stays a plain dict.
    """
    if await health_check():
        return {"status": "ready"}

    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "unavailable"}
