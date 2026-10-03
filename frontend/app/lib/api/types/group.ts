/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type AIProviderProtocol = "openai" | "anthropic";
export type AIProviderSlot = "default" | "image" | "audio" | "planner" | "fast" | "embedding";
export type SupportedMigrations =
  | "nextcloud"
  | "chowdown"
  | "copymethat"
  | "paprika"
  | "mealie_alpha"
  | "tandoor"
  | "plantoeat"
  | "myrecipebox"
  | "recipekeeper"
  | "cookn";

export interface AIProviderCreate {
  name: string;
  baseUrl?: string | null;
  model: string;
  timeout?: number;
  protocol?: AIProviderProtocol;
  monthlyTokenLimit?: number | null;
  requestHeaders?: {
    [k: string]: string;
  };
  requestParams?: {
    [k: string]: string;
  };
}
export interface AIProviderModelInfo {
  id: string;
  displayName: string | null;
  supportsImages: boolean | null;
}
export interface AIProviderModelsQuery {
  protocol?: AIProviderProtocol;
  baseUrl?: string | null;
  timeout?: number;
  requestHeaders?: {
    [k: string]: string;
  };
  requestParams?: {
    [k: string]: string;
  };
}
export interface AIProviderOut {
  name: string;
  baseUrl?: string | null;
  model: string;
  timeout?: number;
  protocol?: AIProviderProtocol;
  monthlyTokenLimit?: number | null;
  requestHeaders?: {
    [k: string]: string;
  };
  requestParams?: {
    [k: string]: string;
  };
  id: string;
}
export interface AIProviderRouteOut {
  id: string;
  settingsId: string;
  slot: AIProviderSlot;
  position: number;
  providerId: string;
}
export interface AIProviderRoutesOut {
  routes: {
    [k: string]: string[];
  };
}
export interface AIProviderRoutesUpdate {
  routes?: {
    [k: string]: string[];
  };
}
export interface AIProviderSave {
  name: string;
  baseUrl?: string | null;
  model: string;
  timeout?: number;
  protocol?: AIProviderProtocol;
  monthlyTokenLimit?: number | null;
  requestHeaders?: {
    [k: string]: string;
  };
  requestParams?: {
    [k: string]: string;
  };
  settingsId: string;
}
export interface AIProviderSettingsCreate {
  groupId: string;
}
export interface AIProviderSettingsOut {
  defaultProviderId: string | null;
  audioProviderId: string | null;
  imageProviderId: string | null;
  providers: AIProviderSummary[];
  aiEnabled: boolean;
  audioProviderEnabled: boolean;
  imageProviderEnabled: boolean;
  ocrFallbackEnabled: boolean;
}
export interface AIProviderSummary {
  id: string;
  name: string;
}
export interface AIProviderSettingsUpdate {
  defaultProviderId: string | null;
  audioProviderId: string | null;
  imageProviderId: string | null;
}
export interface AIProviderTestResult {
  success: boolean;
  message?: string | null;
  supportsImages?: boolean | null;
}
export interface AIProviderUpdate {
  name: string;
  baseUrl?: string | null;
  model: string;
  timeout?: number;
  protocol?: AIProviderProtocol;
  monthlyTokenLimit?: number | null;
  requestHeaders?: {
    [k: string]: string;
  };
  requestParams?: {
    [k: string]: string;
  };
}
export interface AIUsageDaySummary {
  date: string;
  requests: number;
  promptTokens: number;
  completionTokens: number;
}
export interface AIUsageLogCreate {
  groupId?: string | null;
  providerId?: string | null;
  providerName: string;
  model: string;
  protocol: AIProviderProtocol;
  slot: AIProviderSlot;
  feature?: string | null;
  promptTokens?: number;
  completionTokens?: number;
  latencyMs?: number;
  success: boolean;
  errorType?: string | null;
}
export interface AIUsageLogOut {
  groupId: string;
  providerId?: string | null;
  providerName: string;
  model: string;
  protocol: AIProviderProtocol;
  slot: AIProviderSlot;
  feature?: string | null;
  promptTokens?: number;
  completionTokens?: number;
  latencyMs?: number;
  success: boolean;
  errorType?: string | null;
  id: string;
  createdAt?: string | null;
}
export interface AIUsageProviderSummary {
  providerId: string | null;
  providerName: string;
  model: string;
  requests: number;
  failures: number;
  promptTokens: number;
  completionTokens: number;
  monthlyTokenLimit: number | null;
  lastUsedAt: string | null;
}
export interface AIUsageSummary {
  start: string;
  end: string;
  byProvider: AIUsageProviderSummary[];
  byDay: AIUsageDaySummary[];
}
export interface CreateGroupPreferences {
  privateGroup?: boolean;
  showAnnouncements?: boolean;
  groupId: string;
}
export interface DataMigrationCreate {
  sourceType: SupportedMigrations;
}
export interface GroupAdminUpdate {
  id: string;
  name: string;
  preferences?: UpdateGroupPreferences | null;
  aiProviderSettings?: AIProviderSettingsUpdate | null;
}
export interface UpdateGroupPreferences {
  privateGroup?: boolean;
  showAnnouncements?: boolean;
}
export interface GroupDataExport {
  id: string;
  groupId: string;
  name: string;
  filename: string;
  path: string;
  size: string;
  expires: string;
}
export interface GroupStorage {
  usedStorageBytes: number;
  usedStorageStr: string;
  totalStorageBytes: number;
  totalStorageStr: string;
}
export interface ReadGroupPreferences {
  privateGroup?: boolean;
  showAnnouncements?: boolean;
  groupId: string;
  id: string;
}
export interface SeederConfig {
  locale: string;
}
