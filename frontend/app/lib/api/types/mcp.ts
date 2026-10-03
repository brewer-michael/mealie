/* tslint:disable */

/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type McpScope = "mcp:read" | "mcp:write";

export interface McpApiTokenGrantOut {
  tokenId: number;
  allowWrites: boolean;
}
export interface McpApiTokenGrantUpdate {
  allowWrites: boolean;
}
export interface McpClientBase {
  name: string;
  redirectUris: string[];
  pkceOptional?: boolean;
  allowWriteScope?: boolean;
}
export interface McpClientCreate {
  name: string;
  redirectUris: string[];
  pkceOptional?: boolean;
  allowWriteScope?: boolean;
  isConfidential?: boolean;
}
export interface McpClientCreated {
  id: string;
  groupId: string;
  name: string;
  clientId: string;
  isConfidential: boolean;
  pkceOptional: boolean;
  allowWriteScope: boolean;
  redirectUris: string[];
  createdBy?: string | null;
  createdAt?: string | null;
  lastUsedAt?: string | null;
  clientSecret?: string | null;
}
export interface McpClientOut {
  id: string;
  groupId: string;
  name: string;
  clientId: string;
  isConfidential: boolean;
  pkceOptional: boolean;
  allowWriteScope: boolean;
  redirectUris: string[];
  createdBy?: string | null;
  createdAt?: string | null;
  lastUsedAt?: string | null;
}
export interface McpClientSecretOut {
  clientId: string;
  clientSecret: string;
}
export interface McpClientUpdate {
  name: string;
  redirectUris: string[];
  pkceOptional?: boolean;
  allowWriteScope?: boolean;
}
export interface McpConnectionOut {
  clientId: string;
  clientName: string;
  scopes: McpScope[];
  createdAt: string;
  lastUsedAt?: string | null;
}
export interface McpOAuthDecision {
  approve: boolean;
  allowWrites?: boolean;
}
export interface McpOAuthDecisionOut {
  redirectTo: string;
}
export interface McpOAuthRequestOut {
  clientName: string;
  scopes: McpScope[];
  writesOffered: boolean;
  redirectHost: string;
  expiresAt: string;
}
