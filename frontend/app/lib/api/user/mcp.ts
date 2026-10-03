import { BaseAPI } from "../base/base-clients";
import type {
  McpApiTokenGrantOut,
  McpApiTokenGrantUpdate,
  McpClientCreate,
  McpClientCreated,
  McpClientOut,
  McpClientSecretOut,
  McpClientUpdate,
  McpConnectionOut,
  McpOAuthDecision,
  McpOAuthDecisionOut,
  McpOAuthRequestOut,
} from "~/lib/api/types/mcp";

const routes = {
  clients: "/api/groups/mcp/clients",
  clientsId: (id: string) => `/api/groups/mcp/clients/${id}`,
  clientsIdRotateSecret: (id: string) => `/api/groups/mcp/clients/${id}/rotate-secret`,
  presetHomeAssistant: "/api/groups/mcp/presets/home-assistant",
  requestsHandle: (handle: string) => `/api/oauth/requests/${encodeURIComponent(handle)}`,
  connections: "/api/users/self/mcp/connections",
  connectionsClientId: (clientId: string) => `/api/users/self/mcp/connections/${clientId}`,
  apiTokensTokenId: (tokenId: number) => `/api/users/self/mcp/api-tokens/${tokenId}`,
};

/** Mealie's MCP server (docs/ai/PHASE3.md §6): OAuth clients, consent, connected apps and API token write grants */
export class McpAPI extends BaseAPI {
  // ==========================================
  // The group's OAuth clients (managers only)

  async getClients() {
    return await this.requests.get<McpClientOut[]>(routes.clients);
  }

  /** A confidential client's `clientSecret` is in this response only */
  async createClient(payload: McpClientCreate) {
    return await this.requests.post<McpClientCreated>(routes.clients, payload);
  }

  async updateClient(id: string, payload: McpClientUpdate) {
    return await this.requests.put<McpClientOut, McpClientUpdate>(routes.clientsId(id), payload);
  }

  /** Also revokes every token issued to the client */
  async deleteClient(id: string) {
    return await this.requests.delete<McpClientOut>(routes.clientsId(id));
  }

  /** The new secret is in this response only; the old one stops working */
  async rotateClientSecret(id: string) {
    return await this.requests.post<McpClientSecretOut>(routes.clientsIdRotateSecret(id), undefined);
  }

  /** `homeAssistantUrl` replaces the default `http://homeassistant.local:8123` in the second redirect URI */
  async getHomeAssistantPreset(homeAssistantUrl?: string) {
    return await this.requests.get<McpClientCreate>(routes.presetHomeAssistant, { homeAssistantUrl });
  }

  // ==========================================
  // Consent

  async getRequest(handle: string) {
    return await this.requests.get<McpOAuthRequestOut>(routes.requestsHandle(handle));
  }

  async decideRequest(handle: string, decision: McpOAuthDecision) {
    return await this.requests.post<McpOAuthDecisionOut>(routes.requestsHandle(handle), decision);
  }

  // ==========================================
  // The user's connected apps and API token write grants

  async getConnections() {
    return await this.requests.get<McpConnectionOut[]>(routes.connections);
  }

  /** `clientId` is the client's `id` */
  async disconnect(clientId: string) {
    return await this.requests.delete<McpConnectionOut>(routes.connectionsClientId(clientId));
  }

  async getApiTokenGrant(tokenId: number) {
    return await this.requests.get<McpApiTokenGrantOut>(routes.apiTokensTokenId(tokenId));
  }

  async updateApiTokenGrant(tokenId: number, payload: McpApiTokenGrantUpdate) {
    return await this.requests.put<McpApiTokenGrantOut, McpApiTokenGrantUpdate>(
      routes.apiTokensTokenId(tokenId),
      payload,
    );
  }
}
