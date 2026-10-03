"""The MCP server's OAuth tables survive a backup and restore (docs/ai/PHASE3.md §5)"""

import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.core.config import get_app_settings
from mealie.db.db_setup import session_context
from mealie.db.models.ai_mcp import McpOAuthToken
from mealie.services.ai.mcp.auth import clear_mcp_principal_cache, verify_mcp_token
from mealie.services.backups_v2.backup_v2 import BackupV2
from mealie.services.oauth.tokens import hash_secret
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser
from tests.utils.mcp_oauth import HA_LOCAL_REDIRECT_URI, HA_REDIRECT_URI, MCP_URL, connect, create_client, refresh


def test_mcp_oauth_rows_survive_a_backup(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)
    rotated = refresh(api_client, client, tokens["refresh_token"]).json()

    def token_times(token: str) -> tuple:
        with session_context() as session:
            row = session.execute(
                sa.select(McpOAuthToken).where(McpOAuthToken.token_hash == hash_secret(token))
            ).scalar_one()
            return row.expires_at, row.granted_at, row.revoked_at, row.created_at

    before = {token: token_times(token) for token in (tokens["refresh_token"], rotated["access_token"])}

    backup_v2 = BackupV2(get_app_settings().DB_URL)
    try:
        backup_v2.restore(backup_v2.backup())
    finally:
        backup_v2.db_exporter.engine.dispose()

    clear_mcp_principal_cache()
    assert {token: token_times(token) for token in before} == before

    response = api_client.get(api_routes.groups_mcp_clients_item_id(client["id"]), headers=unique_user.token)
    assert response.status_code == 200
    assert response.json()["redirectUris"] == [HA_REDIRECT_URI, HA_LOCAL_REDIRECT_URI]
    assert response.json()["clientId"] == client["clientId"]

    assert verify_mcp_token(rotated["access_token"], MCP_URL) is not None
    assert refresh(api_client, client, rotated["refresh_token"]).status_code == 200
    # the rotated refresh token is still recognised as reused
    assert refresh(api_client, client, tokens["refresh_token"]).json()["error"] == "invalid_grant"
