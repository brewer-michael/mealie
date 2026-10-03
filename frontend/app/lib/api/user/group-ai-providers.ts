import { BaseAPI } from "../base/base-clients";
import type {
  AIProviderCreate,
  AIProviderModelInfo,
  AIProviderModelsQuery,
  AIProviderOut,
  AIProviderRoutesOut,
  AIProviderRoutesUpdate,
  AIProviderTestResult,
  AIProviderUpdate,
  AIUsageSummary,
} from "~/lib/api/types/group";

const prefix = "/api/groups/ai-providers";

const routes = {
  providers: `${prefix}/providers`,
  providersId: (id: string) => `${prefix}/providers/${id}`,
  providersTest: `${prefix}/providers/test`,
  providersIdTest: (id: string) => `${prefix}/providers/${id}/test`,
  providersModels: `${prefix}/providers/models`,
  providersIdModels: (id: string) => `${prefix}/providers/${id}/models`,
  routes: `${prefix}/routes`,
  usage: `${prefix}/usage`,
};

export class AIProvidersAPI extends BaseAPI {
  /** The group's providers by name, with whether each saved API key can be read (managers only) */
  async getAll() {
    return await this.requests.get<AIProviderOut[]>(routes.providers);
  }

  async getOne(id: string) {
    return await this.requests.get<AIProviderOut>(routes.providersId(id));
  }

  async createOne(payload: AIProviderCreate) {
    return await this.requests.post<AIProviderOut>(routes.providers, payload);
  }

  async updateOne(id: string, payload: AIProviderUpdate) {
    return await this.requests.put<AIProviderOut, AIProviderUpdate>(routes.providersId(id), payload);
  }

  async deleteOne(id: string) {
    return await this.requests.delete<AIProviderOut>(routes.providersId(id));
  }

  async testOne(payload: AIProviderCreate) {
    return await this.requests.post<AIProviderTestResult>(routes.providersTest, payload);
  }

  async testSavedOne(id: string, overrides?: AIProviderUpdate & { apiKey?: string }) {
    return await this.requests.post<AIProviderTestResult, typeof overrides>(routes.providersIdTest(id), overrides);
  }

  async listModels(payload: AIProviderModelsQuery & { apiKey: string }) {
    return await this.requests.post<AIProviderModelInfo[]>(routes.providersModels, payload);
  }

  /** A blank `overrides.apiKey` lists the models with the provider's saved key */
  async listSavedModels(id: string, overrides?: AIProviderModelsQuery & { apiKey?: string }) {
    return await this.requests.post<AIProviderModelInfo[]>(routes.providersIdModels(id), overrides);
  }

  async getRoutes() {
    return await this.requests.get<AIProviderRoutesOut>(routes.routes);
  }

  async updateRoutes(payload: AIProviderRoutesUpdate) {
    return await this.requests.put<AIProviderRoutesOut, AIProviderRoutesUpdate>(routes.routes, payload);
  }

  /** Usage from `start` (inclusive) to `end` (exclusive); without either, the current UTC month */
  async getUsage(start?: string, end?: string) {
    return await this.requests.get<AIUsageSummary>(routes.usage, { start, end });
  }
}
