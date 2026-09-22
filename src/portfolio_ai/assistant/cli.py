"""``python -m portfolio_ai.assistant`` -- talk to Rachel from a terminal.

This is how the assistant gets reviewed before there is a web page to review it in:
ask the same questions here and on the live site, and compare. It prints what a
visitor would never see -- the route each message took, the query the model wrote,
what the best chunk scored, and the bill -- because that is what a change to any of
it has to be judged on.

Two Python details worth knowing, because both are easy to get wrong:

**One event loop for the whole session.** ``asyncio.run`` creates a loop, runs a
coroutine and closes the loop. Calling it per question would work exactly once: the
connection pool binds to the loop that opened it, and the next question would find
its connections attached to a loop that no longer exists. ``asyncio.Runner`` keeps
one loop open across many ``run()`` calls, which is precisely this situation.

**``input()`` stays on the main thread.** Reading a line blocks, and blocking inside
async code is the trap lesson 5 is about -- but the usual fix, ``asyncio.to_thread``,
is wrong here: Ctrl-C at the prompt would leave a thread blocked on stdin and the
interpreter would wait for it on the way out. Keeping the loop out of the way while
waiting for input, and only entering it to answer, avoids both problems.
"""

import asyncio
import uuid
from typing import Annotated

import typer

from portfolio_ai.assistant import agent, conversation, memory
from portfolio_ai.assistant.agent import Done, Searched, Token, TurnResult
from portfolio_ai.assistant.memory import HistoryMessage
from portfolio_ai.config import get_settings
from portfolio_ai.db.pool import close_pool
from portfolio_ai.exceptions import PortfolioAIError
from portfolio_ai.llm.client import close_client
from portfolio_ai.logging import configure_logging

app = typer.Typer(add_completion=False, help="Chat with Rachel against the knowledge base.")

DIM = typer.colors.BRIGHT_BLACK
SEPARATOR = " · "  # a middle dot, written as an escape so it cannot be mistyped
TOP_CHUNKS_SHOWN = 5


def _summary(result: TurnResult) -> str:
    """The line printed under every answer: route, evidence, cost, timings."""
    # Annotated, because the first element is a Classification and mypy would
    # otherwise decide the list may only ever hold those three words.
    parts: list[str] = [result.classification]

    for search in result.searches:
        score = f" top {search.top_score:.2f}" if search.top_score is not None else " nothing"
        parts.append(f'searched "{search.query}"{score}')

    cached = sum(call.cached_tokens for call in result.calls)
    tokens = f"{result.prompt_tokens:,} in"
    if cached:
        tokens += f" ({cached:,} cached)"
    parts.append(f"{tokens} / {result.completion_tokens:,} out")
    calls = len(result.calls)
    parts.append(f"{calls} call" if calls == 1 else f"{calls} calls")
    parts.append(f"${result.cost:.4f}")

    if result.first_token_ms is not None:
        parts.append(f"first word {result.first_token_ms / 1000:.1f}s")
    parts.append(f"{result.latency_ms / 1000:.1f}s")

    if result.fallback_used:
        parts.append("fallback")
    if result.links_removed:
        parts.append(f"{result.links_removed} link(s) removed")
    if result.message_id is not None:
        parts.append(f"message {result.message_id}")

    return SEPARATOR.join(parts)


class _Chat:
    """One terminal conversation, saved or not."""

    def __init__(self, *, save: bool, verbose: bool, session_id: str) -> None:
        self.save = save
        self.verbose = verbose
        self.session_id = session_id
        # Only used when nothing is being saved; otherwise history comes from the
        # database, exactly as it will for a visitor.
        self.history: list[HistoryMessage] = []

    def restart(self) -> None:
        self.session_id = str(uuid.uuid4())
        self.history.clear()
        typer.secho(f"new session {self.session_id}", fg=DIM)

    async def ask(self, message: str) -> None:
        window = get_settings().memory_window_turns
        events = (
            conversation.chat(self.session_id, message)
            if self.save
            else agent.respond(message, memory.window(self.history, window))
        )

        typer.secho("\nrachel> ", fg=typer.colors.CYAN, nl=False)

        async for event in events:
            match event:
                case Token(text):
                    typer.echo(text, nl=False)
                case Searched() if self.verbose:
                    _show_search(event)
                case Done(result):
                    typer.echo()
                    typer.secho(f"  {_summary(result)}", fg=DIM)
                    if not self.save:
                        self.history += [
                            HistoryMessage("user", message),
                            HistoryMessage("assistant", result.reply),
                        ]
                case _:
                    pass


def _show_search(event: Searched) -> None:
    """With --verbose: what the search asked for and the best of what came back."""
    typer.secho(f'\n  searched "{event.query}"', fg=DIM)
    for chunk in event.chunks[:TOP_CHUNKS_SHOWN]:
        typer.secho(f"    {chunk.score:.3f}  {chunk.title} / {chunk.section_title}", fg=DIM)
    typer.secho("  rachel> ", fg=DIM, nl=False)


async def _shutdown() -> None:
    await close_client()
    await close_pool()


def _ask(runner: asyncio.Runner, chat: _Chat, message: str) -> bool:
    """Run one turn. Returns False if it failed for a reason worth an exit code."""
    try:
        runner.run(chat.ask(message))
    except PortfolioAIError as exc:
        # Something we refused or could not do -- a rejected key, OpenAI unreachable.
        # The message is written for a person, so print it and carry on.
        typer.secho(f"\n{exc}", fg=typer.colors.RED, err=True)
        return False
    except KeyboardInterrupt:
        typer.secho("\n(interrupted)", fg=DIM)
    return True


def _repl(runner: asyncio.Runner, chat: _Chat) -> None:
    typer.secho(f"session {chat.session_id}{'' if chat.save else '  (nothing is saved)'}", fg=DIM)
    typer.secho("/new starts a new session, /exit leaves", fg=DIM)

    while True:
        try:
            line = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo()
            return

        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return
        if line == "/new":
            chat.restart()
            continue

        _ask(runner, chat, line)


@app.command()
def main(
    message: Annotated[
        str | None,
        typer.Option("--message", "-m", help="Ask one question, print the answer, exit."),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", help="Continue an existing session id."),
    ] = None,
    no_save: Annotated[
        bool,
        typer.Option("--no-save", help="Do not read or write the chat tables."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show what each search found."),
    ] = False,
) -> None:
    """Chat with Rachel against the knowledge base in the configured database."""
    settings = get_settings()

    # Production holds real visitors' conversations, and analytics read every row of
    # it. A smoke test typed into that table is noise that cannot be told apart from
    # a visitor afterwards, so in production this only runs with --no-save.
    if settings.environment != "local" and not no_save:
        raise typer.BadParameter(
            f"ENVIRONMENT is {settings.environment!r}. Use --no-save: this would otherwise "
            "write test questions into real visitors' chat history.",
            param_hint="--no-save",
        )

    # Quieter than the default: one JSON line per OpenAI call in the middle of a
    # conversation makes the conversation unreadable. Warnings still appear.
    configure_logging("WARNING")

    chat = _Chat(save=not no_save, verbose=verbose, session_id=session or str(uuid.uuid4()))

    with asyncio.Runner() as runner:
        try:
            if message is not None:
                if not _ask(runner, chat, message):
                    raise typer.Exit(1)
                return
            _repl(runner, chat)
        finally:
            runner.run(_shutdown())


if __name__ == "__main__":  # pragma: no cover
    app()
