"""``POST /v1/messages/{message_id}/feedback`` -- a thumbs up or down on an answer."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from portfolio_ai.api.schemas import FeedbackRequest
from portfolio_ai.api.security import require_api_key
from portfolio_ai.db import feedback as feedback_db

# The largest value a Postgres bigint holds. An id past it is rejected as a 422
# here rather than reaching psycopg, which would fail to send it and turn a typo
# into a 500.
MAX_MESSAGE_ID = 2**63 - 1

router = APIRouter(prefix="/v1", tags=["feedback"], dependencies=[Depends(require_api_key)])


@router.post("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def rate_answer(
    message_id: Annotated[int, Path(gt=0, le=MAX_MESSAGE_ID)],
    body: FeedbackRequest,
) -> None:
    """Record a rating: 1 for up, -1 for down. Voting again replaces the vote.

    404 unless ``message_id`` is an answer in the conversation ``session_id`` names.
    One answer covers every way that can fail -- a missing id, a question's id,
    somebody else's conversation -- so the response says nothing about which, and
    nothing about whether an id exists.
    """
    stored = await feedback_db.record_feedback(
        session_id=body.session_id,
        message_id=message_id,
        rating=body.rating,
        # An empty comment is no comment. Stripped by the schema, so "   " is "" here.
        comment=body.comment or None,
    )
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="There is no such answer in this conversation.",
        )
