"""
Fixes for databases, and backups of them, created by earlier builds of this fork.

The fork's old `add-ocr-recipe` build added an alembic revision, `add_admin_settings`, on top of
upstream's `e6bb583aac2d`. It created an `admin_settings` table that nothing uses any more. That
revision is not in upstream's migration graph, so alembic refuses to upgrade such a database
("Can't locate revision identified by 'add_admin_settings'"). `fix_legacy_fork_revision` runs
before `alembic upgrade` and rewinds the database onto the upstream graph; `fix_legacy_fork_backup`
does the same for the database dump in a backup made by that build, before it is restored.
"""

from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.orm import Session

from mealie.core import root_logger

LEGACY_REVISION = "add_admin_settings"
LEGACY_DOWN_REVISION = "e6bb583aac2d"
LEGACY_TABLE = "admin_settings"

logger = root_logger.get_logger("init_db")


def fix_legacy_fork_revision(session: Session) -> bool:
    """
    Drops the old fork's `admin_settings` table and resets `alembic_version` to its upstream
    parent revision, so the regular upgrade can carry on from there. Returns True if the
    database needed fixing.

    Any other database (fresh, without an `alembic_version` table, or on an upstream revision)
    is left untouched. The session's transaction is always ended, so it holds no locks while
    alembic migrates over its own connection.
    """
    try:
        # Returns () when the alembic_version table doesn't exist yet
        heads = MigrationContext.configure(session.connection()).get_current_heads()
        is_legacy = LEGACY_REVISION in heads

        if is_legacy:
            logger.warning(
                f"Database is on revision '{LEGACY_REVISION}' from an older build of this fork. "
                f"Dropping its unused '{LEGACY_TABLE}' table (any AI provider settings stored there must be "
                f"configured again) and resetting the revision to '{LEGACY_DOWN_REVISION}'."
            )

            # The UPDATE must come first: pysqlite only opens a transaction before DML, so a leading
            # DROP would be committed on its own and could not be rolled back with the UPDATE.
            session.execute(
                text("UPDATE alembic_version SET version_num = :upstream WHERE version_num = :legacy"),
                {"upstream": LEGACY_DOWN_REVISION, "legacy": LEGACY_REVISION},
            )
            session.execute(text(f"DROP TABLE IF EXISTS {LEGACY_TABLE}"))

        session.commit()
    except Exception:
        session.rollback()
        raise

    return is_legacy


def fix_legacy_fork_backup(db_dump: dict[str, list[dict]]) -> bool:
    """
    Backup counterpart of `fix_legacy_fork_revision`: rewrites a backup's database dump in place,
    resetting its `alembic_version` to the upstream parent revision and removing the `admin_settings`
    rows, which have no table to be restored into. Returns True if the dump needed fixing.

    Any other dump is left untouched.
    """
    versions = db_dump.get("alembic_version", [])
    is_legacy = any(row.get("version_num") == LEGACY_REVISION for row in versions)

    if is_legacy:
        logger.warning(
            f"Backup is on revision '{LEGACY_REVISION}' from an older build of this fork. Skipping its "
            f"'{LEGACY_TABLE}' table (any AI provider settings stored there must be configured again) and "
            f"restoring it as revision '{LEGACY_DOWN_REVISION}'."
        )

        for row in versions:
            if row.get("version_num") == LEGACY_REVISION:
                row["version_num"] = LEGACY_DOWN_REVISION
        db_dump.pop(LEGACY_TABLE, None)

    return is_legacy
