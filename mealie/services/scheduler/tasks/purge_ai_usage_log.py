from datetime import UTC, datetime, timedelta

from mealie.core import root_logger
from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories

AI_USAGE_RETENTION_DAYS = 400
"""How long AI usage log rows are kept: a little over a year, so a full year can always be compared"""


def purge_ai_usage_log(retention_days: int = AI_USAGE_RETENTION_DAYS) -> None:
    """Deletes AI usage log rows older than `retention_days`, across all groups"""
    logger = root_logger.get_logger()
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    with session_context() as session:
        repos = get_repositories(session, group_id=None, household_id=None)
        removed = repos.group_ai_usage.purge_older_than(cutoff)

    logger.info(f"purged {removed} AI usage log row(s) older than {retention_days} days")
