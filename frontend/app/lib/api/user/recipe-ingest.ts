import type { AxiosProgressEvent, AxiosRequestConfig } from "axios";
import { BaseAPI } from "../base/base-clients";
import { type QueryValue, route } from "../base/route";
import type {
  AINotifierEventsOut,
  AINotifierEventsUpdate,
  CardDraftSaved,
  CardDraftUpdate,
  CommitOut,
  CommitRequest,
  EvalCaseOut,
  EvalCaseRequest,
  EvalCaseSummary,
  IngestAbout,
  IngestResponse,
  IngestStatus,
  PageOut,
  RecipeIngestionBatchOut,
  RecipeIngestionJobCounts,
  RecipeIngestionJobOut,
  RecipeIngestionJobPagination,
  RecipeIngestionJobState,
  RecipeIngestionSettingsOut,
  RecipeIngestionSettingsUpdate,
  RereadRequest,
  RotateRequest,
} from "~/lib/api/types/recipe-ingest";

const prefix = "/api/ai/ingest";

export type PageImageKind = "page" | "view" | "thumb";

const routes = {
  ingest: prefix,
  batches: `${prefix}/batches`,
  batchesId: (id: string) => `${prefix}/batches/${id}`,
  batchesIdSeal: (id: string) => `${prefix}/batches/${id}/seal`,
  jobs: `${prefix}/jobs`,
  jobsCounts: `${prefix}/jobs/counts`,
  jobsId: (id: string) => `${prefix}/jobs/${id}`,
  jobsIdState: (id: string) => `${prefix}/jobs/${id}/state`,
  jobsIdReextract: (id: string) => `${prefix}/jobs/${id}/reextract`,
  jobsIdReread: (id: string) => `${prefix}/jobs/${id}/reread`,
  jobsIdRetry: (id: string) => `${prefix}/jobs/${id}/retry`,
  jobsIdCancel: (id: string) => `${prefix}/jobs/${id}/cancel`,
  jobsIdPagesNRotate: (id: string, n: number) => `${prefix}/jobs/${id}/pages/${n}/rotate`,
  jobsIdPagesNImage: (id: string, n: number, kind: PageImageKind) => `${prefix}/jobs/${id}/pages/${n}/${kind}`,
  jobsIdCommit: (id: string) => `${prefix}/jobs/${id}/commit`,
  jobsIdEvalCase: (id: string) => `${prefix}/jobs/${id}/eval-case`,
  evalCases: `${prefix}/eval-cases`,
  evalCasesSlug: (slug: string) => `${prefix}/eval-cases/${encodeURIComponent(slug)}`,
  settings: `${prefix}/settings`,
  notifiersIdEvents: (id: string) => `/api/ai/notifiers/${id}/events`,
  notifiersIdEventsTest: (id: string) => `/api/ai/notifiers/${id}/events/test`,
  about: "/api/ai/about",
};

/** Options sent with a card's photos (form fields of the multipart upload) */
export interface RecipeIngestUploadOptions {
  /** The app's batch; `"new"` forces a new one; left out, an API upload joins a recent one */
  batchId?: string | null;
  /** The card's capture order in the batch */
  position?: number | null;
  /** Make every photo its own card (by default the photos are one card, front first) */
  split?: boolean;
  /** Keep this card on this server (only local AI providers read it) */
  localOnly?: boolean;
  /** Queue the card even if the same photos were scanned before */
  allowDuplicate?: boolean;
}

export interface RecipeIngestUploadConfig {
  onUploadProgress?: (event: AxiosProgressEvent) => void;
  signal?: AbortSignal;
  /** Don't toast the error's `detail.message` (for an attempt that will be retried) */
  suppressAlert?: boolean;
}

export interface RecipeIngestJobsQuery {
  status?: IngestStatus | IngestStatus[] | null;
  batchId?: string | null;
  page?: number;
  perPage?: number;
}

/** The form a card's photos are sent as: one `files` part per photo, front first, and the options as text fields */
export function buildIngestForm(files: readonly Blob[], options: RecipeIngestUploadOptions = {}): FormData {
  const form = new FormData();
  files.forEach((file, index) => {
    const name = file instanceof File && file.name ? file.name : `photo-${index + 1}.jpg`;
    form.append("files", file, name);
  });
  if (options.batchId) {
    form.append("batchId", options.batchId);
  }
  if (options.position !== undefined && options.position !== null) {
    form.append("position", String(options.position));
  }
  if (options.split) {
    form.append("split", "true");
  }
  if (options.localOnly) {
    form.append("localOnly", "true");
  }
  if (options.allowDuplicate) {
    form.append("allowDuplicate", "true");
  }
  return form;
}

/** Recipe card ingestion (docs/ai/PHASE2.md §14): uploads, batches, review, commit, settings and notifier toggles */
export class RecipeIngestAPI extends BaseAPI {
  // ==========================================
  // Uploads and batches

  /** One card (or, with `split`, one card per photo); answers 202 with the jobs and any rejected photos */
  async upload(files: readonly Blob[], options: RecipeIngestUploadOptions = {}, config: RecipeIngestUploadConfig = {}) {
    const requestConfig: AxiosRequestConfig = {
      onUploadProgress: config.onUploadProgress,
      signal: config.signal,
      suppressAlert: config.suppressAlert,
    };
    return await this.requests.post<IngestResponse>(routes.ingest, buildIngestForm(files, options), requestConfig);
  }

  async createBatch() {
    return await this.requests.post<RecipeIngestionBatchOut>(routes.batches, {});
  }

  /** Marks a batch done; send it once every card of the batch has uploaded or failed for good */
  async sealBatch(id: string) {
    return await this.requests.post<RecipeIngestionBatchOut>(routes.batchesIdSeal(id), {});
  }

  async getBatch(id: string) {
    return await this.requests.get<RecipeIngestionBatchOut>(routes.batchesId(id));
  }

  // ==========================================
  // Jobs

  async getJobs(query: RecipeIngestJobsQuery = {}) {
    const params: Record<string, QueryValue> = {};
    if (query.status) {
      // repeated (`status=a&status=b`), as FastAPI reads a list
      params.status = Array.isArray(query.status) ? query.status : [query.status];
    }
    if (query.batchId) {
      params.batchId = query.batchId;
    }
    if (query.page) {
      params.page = query.page;
    }
    if (query.perPage) {
      params.perPage = query.perPage;
    }
    return await this.requests.get<RecipeIngestionJobPagination>(route(routes.jobs, params));
  }

  async getCounts() {
    return await this.requests.get<RecipeIngestionJobCounts>(routes.jobsCounts);
  }

  async getJob(id: string) {
    return await this.requests.get<RecipeIngestionJobOut>(routes.jobsId(id));
  }

  async getJobState(id: string) {
    return await this.requests.get<RecipeIngestionJobState>(routes.jobsIdState(id));
  }

  /** Saves the draft; a stale `draftVersion` is a 409 whose `detail.code` is `version_conflict` */
  async updateJob(id: string, payload: CardDraftUpdate) {
    return await this.requests.put<CardDraftSaved, CardDraftUpdate>(routes.jobsId(id), payload);
  }

  async reextract(id: string) {
    return await this.requests.post<RecipeIngestionJobState>(routes.jobsIdReextract(id), {});
  }

  /** A 409 whose `detail.code` is `busy` means a task is already running: queue the re-read and send it later */
  async reread(id: string, payload: RereadRequest) {
    return await this.requests.post<RecipeIngestionJobState>(routes.jobsIdReread(id), payload);
  }

  async retry(id: string) {
    return await this.requests.post<RecipeIngestionJobState>(routes.jobsIdRetry(id), {});
  }

  async cancel(id: string) {
    return await this.requests.post<RecipeIngestionJobState>(routes.jobsIdCancel(id), {});
  }

  async rotatePage(id: string, page: number, payload: RotateRequest) {
    return await this.requests.post<PageOut>(routes.jobsIdPagesNRotate(id, page), payload);
  }

  /** A page image's URL, for `<img src>` (it authenticates with the session cookie) */
  pageImageUrl(id: string, page: number, kind: PageImageKind) {
    return routes.jobsIdPagesNImage(id, page, kind);
  }

  /** 201 with the new recipe; 200 if it was already committed; 422 `unresolved_flags`; 409 otherwise */
  async commit(id: string, payload: CommitRequest) {
    return await this.requests.post<CommitOut>(routes.jobsIdCommit(id), payload);
  }

  async discard(id: string) {
    return await this.requests.delete<null>(routes.jobsId(id));
  }

  // ==========================================
  // Eval cases (group managers)

  async saveEvalCase(id: string, payload: EvalCaseRequest) {
    return await this.requests.post<EvalCaseOut>(routes.jobsIdEvalCase(id), payload);
  }

  async getEvalCases() {
    return await this.requests.get<EvalCaseSummary[]>(routes.evalCases);
  }

  async deleteEvalCase(slug: string) {
    return await this.requests.delete<null>(routes.evalCasesSlug(slug));
  }

  // ==========================================
  // Settings, notifiers and what the server offers

  async getSettings() {
    return await this.requests.get<RecipeIngestionSettingsOut>(routes.settings);
  }

  async updateSettings(payload: RecipeIngestionSettingsUpdate) {
    return await this.requests.put<RecipeIngestionSettingsOut, RecipeIngestionSettingsUpdate>(routes.settings, payload);
  }

  async getNotifierEvents(notifierId: string) {
    return await this.requests.get<AINotifierEventsOut>(routes.notifiersIdEvents(notifierId));
  }

  async updateNotifierEvents(notifierId: string, payload: AINotifierEventsUpdate) {
    return await this.requests.put<AINotifierEventsOut, AINotifierEventsUpdate>(
      routes.notifiersIdEvents(notifierId),
      payload,
    );
  }

  async testNotifierEvents(notifierId: string) {
    return await this.requests.post<null>(routes.notifiersIdEventsTest(notifierId), {});
  }

  async getAbout() {
    return await this.requests.get<IngestAbout>(routes.about);
  }
}
