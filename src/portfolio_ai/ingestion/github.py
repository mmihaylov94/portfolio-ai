"""Reading the knowledge base out of GitHub.

Two endpoints, and they are deliberately different kinds of thing.

The **tree API** returns every path in a branch in one response, each with its git
blob SHA. That SHA is the interesting part: it is a hash of the file's contents, so
comparing it against what was stored last run says whether a file changed *without
downloading it*. Eleven small files is not a meaningful saving in bandwidth -- the
point is that it makes "nothing changed" cost exactly one request.

**raw.githubusercontent.com** serves file contents with no API rate limit and no
JSON envelope. It is only asked for files the tree says are new or changed.

Both go through one ``httpx.AsyncClient``, created by the caller and passed in.
That is a deliberate choice over a module-level client: a client owns a connection
pool that has to be closed, the pipeline already has a natural lifetime to tie it
to, and a test can pass a client pointed at a fake transport without patching
anything.
"""

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from portfolio_ai.concurrency import gather_limited
from portfolio_ai.config import get_settings

log = structlog.get_logger(__name__)

_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"


@dataclass(frozen=True)
class RemoteFile:
    """One Markdown file in the source repository, as the tree API describes it."""

    path: str
    blob_sha: str

    @property
    def raw_url(self) -> str:
        settings = get_settings()
        return f"{_RAW}/{settings.github_repo}/{settings.github_branch}/{self.path}"


def build_client() -> httpx.AsyncClient:
    """An HTTP client configured for GitHub.

    The token is optional. Unauthenticated requests to the API are limited to 60
    per hour per IP address and a run makes exactly one, so the headroom is
    generous -- but the limit is shared with anything else calling GitHub from the
    same address, which on a server running other automation is not nothing. It
    also becomes required rather than optional if the repository is ever private.
    """
    settings = get_settings()

    headers = {
        "Accept": "application/vnd.github+json",
        # GitHub asks for a User-Agent and returns 403 without one. Naming the
        # project means a rate-limit problem can be traced to its cause.
        "User-Agent": "portfolio-ai-ingestion",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if settings.github_token is not None:
        headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"

    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(30.0, connect=10.0),
        # Raw file URLs redirect. Without this the body is an empty 302 and the
        # failure appears later as a document with no frontmatter.
        follow_redirects=True,
    )


async def list_markdown_files(client: httpx.AsyncClient) -> list[RemoteFile]:
    """Every ``.md`` file under the configured docs path, with its blob SHA.

    ``recursive=1`` returns the whole tree in one response rather than one request
    per directory. It has a size limit and sets ``truncated: true`` when it is hit,
    which this checks for -- a truncated tree looks exactly like a repository with
    fewer files, and that is precisely the input that would make the purge step
    delete documents that still exist.
    """
    settings = get_settings()
    url = f"{_API}/repos/{settings.github_repo}/git/trees/{settings.github_branch}"

    response = await client.get(url, params={"recursive": "1"})
    response.raise_for_status()
    payload: dict[str, Any] = response.json()

    if payload.get("truncated"):
        raise RuntimeError(
            f"GitHub truncated the tree for {settings.github_repo}. "
            "Ingestion cannot tell a missing file from a deleted one, so this run is unsafe."
        )

    prefix = f"{settings.github_docs_path.strip('/')}/"

    files = [
        RemoteFile(path=entry["path"], blob_sha=entry["sha"])
        for entry in payload.get("tree", [])
        # "blob" is a file; "tree" is a directory. Filtering on it keeps a folder
        # called `something.md` from being treated as an article.
        if entry.get("type") == "blob"
        and entry["path"].startswith(prefix)
        and entry["path"].endswith(".md")
    ]

    # Sorted so a run's logs and a dry run's plan are in a stable order. The tree
    # API's ordering is not documented as stable, and a diff between two runs is
    # much easier to read when the only differences are real ones.
    files.sort(key=lambda file: file.path)

    log.info(
        "discovered_documents",
        repo=settings.github_repo,
        branch=settings.github_branch,
        path=settings.github_docs_path,
        count=len(files),
    )

    return files


async def fetch_file(client: httpx.AsyncClient, file: RemoteFile) -> str:
    """Download one file's raw contents."""
    response = await client.get(file.raw_url)
    response.raise_for_status()
    return response.text


async def fetch_all(client: httpx.AsyncClient, files: list[RemoteFile]) -> dict[str, str]:
    """Download several files at once, capped by ``INGESTION_CONCURRENCY``.

    Returns a mapping of path to contents. ``gather_limited`` preserves order, so
    zipping the results back against the inputs is safe -- but a dictionary is
    returned anyway, because the caller needs to look files up by path rather than
    by position and the correspondence should not have to be maintained by hand.
    """
    if not files:
        return {}

    settings = get_settings()
    bodies = await gather_limited(
        settings.ingestion_concurrency,
        *(fetch_file(client, file) for file in files),
    )

    return dict(zip((file.path for file in files), bodies, strict=True))
