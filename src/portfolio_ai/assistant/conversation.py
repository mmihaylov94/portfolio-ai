"""One turn of a real conversation: remember, answer, record.

``respond()`` in agent.py answers a message and forgets it. This adds the two
things a conversation needs -- what was said before, and a record of what was said
now -- and is what the API will call for every visitor message.

They are separate for a reason that arrives in step 5: the eval harness runs
hundreds of questions through ``respond()`` and must not fill the chat tables with
questions no visitor asked. Analytics over a table half full of test traffic is
worse than no analytics.
"""

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import replace

from portfolio_ai.assistant import agent
from portfolio_ai.assistant.agent import AssistantConfig, Done, Event
from portfolio_ai.config import get_settings
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db.chat import ClientInfo


async def chat(
    session_id: str,
    message: str,
    *,
    client: ClientInfo | None = None,
    config: AssistantConfig | None = None,
) -> AsyncIterator[Event]:
    """Answer ``message`` in the conversation ``session_id``, and store the turn.

    Yields the same events as :func:`agent.respond`, except that the final
    ``Done`` carries the stored answer's ``message_id`` -- which is what the browser
    needs to attach a thumbs up or down to it later.

    ``client`` is where the conversation came from, as the API was told by the
    proxy in front of it; it is recorded once, when the session is first seen. The
    terminal chat has no such thing and passes nothing.

    No database connection is held while the model is working. The session and its
    history are read before the first call, and the turn is written after the last
    one; in between the pool is free, which matters because those calls take
    seconds and the pool is shared with everything else on the instance.
    """
    settings = get_settings()
    asked_at = dt.datetime.now(dt.UTC)

    session_pk = await chat_db.ensure_session(session_id, client)
    history = await chat_db.load_recent_messages(
        session_pk,
        # Exchanges to messages: a window of 25 turns is the last 50 rows.
        limit=2 * settings.memory_window_turns,
    )

    async for event in agent.respond(message, history, config=config):
        if isinstance(event, Done):
            message_id = await chat_db.insert_turn(session_pk, message, event.result, asked_at)
            yield Done(replace(event.result, message_id=message_id))
        else:
            yield event
