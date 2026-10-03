from datetime import UTC, datetime

from mealie.core import root_logger
from mealie.db.db_setup import session_context
from mealie.repos.repository_mcp import TOKEN_RETENTION, RepositoryMcpOAuth


def purge_mcp_oauth() -> None:
    """
    Deletes the MCP server's expired authorization requests and codes, its tokens expired or revoked more than
    `TOKEN_RETENTION` ago, and rows left behind by deleted clients, users and API tokens (docs/ai/PHASE3.md §5)
    """
    logger = root_logger.get_logger()

    with session_context() as session:
        removed = RepositoryMcpOAuth(session).purge(datetime.now(UTC))

    logger.info(f"purged MCP OAuth rows (tokens are kept {TOKEN_RETENTION.days} days after they end): {removed}")
