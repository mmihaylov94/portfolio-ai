"""``POST /v1/contact`` -- one message from the site's contact form.

Called by the portfolio's own API, which has already checked that the four fields
are there, limited the visitor's address, and verified reCAPTCHA. This endpoint
does the rest: the spam rules, the classifier, the acknowledgement to the visitor
and the notification to the owner (``contact/service.py``).

**200 means the owner has been told.** Whatever was decided about the message --
accepted, blocked as spam, not classified -- the answer is the same 200, because
the notification went out and nothing more is owed to the caller. A visitor is not
told they were taken for spam. Anything else means the message did not get through,
and the site says so:

====================================  ===========================================
the body does not fit                 422
too many submissions this hour        429, Retry-After
shutting down                         503, Retry-After
mail not set up, or the send failed   503
the mail server refused the login     500
====================================  ===========================================
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status

from portfolio_ai.api.schemas import ContactRequest, ContactResponse
from portfolio_ai.api.security import require_api_key
from portfolio_ai.config import get_settings
from portfolio_ai.contact import service
from portfolio_ai.contact.desk import Desk

router = APIRouter(prefix="/v1", tags=["contact"], dependencies=[Depends(require_api_key)])

# How soon to try again after a refusal that will pass by itself.
RETRY_SOON_SECONDS = 5


def desk_of(request: Request) -> Desk:
    """The app's desk: the hour's count and the messages in hand (``contact/desk.py``).

    ``app.state`` holds anything and promises nothing about what it holds, so this
    is where its type is stated.
    """
    desk: Desk = request.app.state.contact
    return desk


@router.post("/contact")
async def contact(request: Request, body: ContactRequest) -> ContactResponse:
    """Take one contact form message, and answer once the owner has been notified."""
    desk = desk_of(request)

    if desk.closing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The service is restarting. Please try again in a moment.",
            headers={"Retry-After": str(RETRY_SOON_SECONDS)},
        )

    wait = desk.admit(get_settings().contact_max_per_hour)
    if wait is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many messages in the last hour. Please try again later.",
            headers={"Retry-After": str(wait)},
        )

    task = desk.start(
        service.handle(
            email=body.email,
            subject=body.subject,
            message=body.message,
            reason=body.reason,
            replies=desk.replies,
        )
    )
    # shield: if this request is cancelled, because the caller hung up, the
    # cancellation stops here and the task carries on to the end. A failure in the
    # task still arrives here as its exception, and errors.py gives it a status.
    await asyncio.shield(task)
    return ContactResponse()
