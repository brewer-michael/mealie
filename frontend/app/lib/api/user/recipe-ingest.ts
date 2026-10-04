import axios from "axios";
import type { AxiosProgressEvent, AxiosRequestConfig, AxiosResponseTransformer } from "axios";
import { BaseAPI } from "../base/base-clients";
import { type QueryValue, route } from "../base/route";
import type { PaginationData } from "../types/non-generated";
import type {
  AINotifierEventsOut,
  AINotifierEventsUpdate,
  BulkCommitOut,
  BulkCommitRequest,
  CardDraftSaved,
  CardDraftUpdate,
  CommitOut,
  CommitRequest,
  EvalCaseOut,
  EvalCaseRequest,
  EvalCaseSummary,
  EvalCaseUpdate,
  IngestAbout,
  IngestResponse,
  IngestStatus,
  MergeRequest,
  PageOut,
  ParseLinesRequest,
  ProposalTarget,
  RebuildRequest,
  RecipeIngestionBatchOut,
  RecipeIngestionJobCounts,
  RecipeIngestionJobOut,
  RecipeIngestionJobState,
  RecipeIngestionJobSummary,
  RecipeIngestionSettingsOut,
  RecipeIngestionSettingsUpdate,
  RegionHintOut,
  RereadRequest,
  RotateRequest,
  UncommitRequest,
} from "~/lib/api/types/recipe-ingest";

const prefix = "/api/ai/ingest";

export type PageImageKind = "page" | "view" | "thumb";

const routes = {
  ingest: prefix,
  batches: `${prefix}/batches`,
  batchesId: (id: string) => `${prefix}/batches/${id}`,
  batchesIdSeal: (id: string) => `${prefix}/batches/${id}/seal`,
  batchesIdTouch: (id: string) => `${prefix}/batches/${id}/touch`,
  batchesIdCommitClean: (id: string) => `${prefix}/batches/${id}/commit-clean`,
  jobs: `${prefix}/jobs`,
  jobsCounts: `${prefix}/jobs/counts`,
  jobsId: (id: string) => `${prefix}/jobs/${id}`,
  jobsIdState: (id: string) => `${prefix}/jobs/${id}/state`,
  jobsIdReextract: (id: string) => `${prefix}/jobs/${id}/reextract`,
  jobsIdReread: (id: string) => `${prefix}/jobs/${id}/reread`,
  jobsIdRetry: (id: string) => `${prefix}/jobs/${id}/retry`,
  jobsIdCancel: (id: string) => `${prefix}/jobs/${id}/cancel`,
  jobsIdReadWithCloud: (id: string) => `${prefix}/jobs/${id}/read-with-cloud`,
  jobsIdMerge: (id: string) => `${prefix}/jobs/${id}/merge`,
  jobsIdRebuild: (id: string) => `${prefix}/jobs/${id}/rebuild`,
  jobsIdParseLines: (id: string) => `${prefix}/jobs/${id}/parse-lines`,
  jobsIdRegionHint: (id: string) => `${prefix}/jobs/${id}/region-hint`,
  jobsIdPagesNRotate: (id: string, n: number) => `${prefix}/jobs/${id}/pages/${n}/rotate`,
  jobsIdPagesNImage: (id: string, n: number, kind: PageImageKind) => `${prefix}/jobs/${id}/pages/${n}/${kind}`,
  jobsIdCommit: (id: string) => `${prefix}/jobs/${id}/commit`,
  jobsIdUncommit: (id: string) => `${prefix}/jobs/${id}/uncommit`,
  jobsIdEvalCase: (id: string) => `${prefix}/jobs/${id}/eval-case`,
  evalCases: `${prefix}/eval-cases`,
  evalCasesSlug: (slug: string) => `${prefix}/eval-cases/${encodeURIComponent(slug)}`,
  evalCasesSlugDownload: (slug: string) => `${prefix}/eval-cases/${encodeURIComponent(slug)}/download`,
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

export interface RecipeIngestRequestConfig {
  /**
   * Don't toast the answer's message, on success or failure: the caller shows the outcome itself (the upload queue,
   * whose attempts retry and whose cards show why they failed)
   */
  suppressAlert?: boolean;
}

export interface RecipeIngestUploadConfig extends RecipeIngestRequestConfig {
  onUploadProgress?: (event: AxiosProgressEvent) => void;
  signal?: AbortSignal;
}

export interface RecipeIngestJobsQuery {
  status?: IngestStatus | IngestStatus[] | null;
  batchId?: string | null;
  /** Only cards added as recipes since then */
  committedSince?: Date | string | null;
  /** `committedAt`: the latest commit first (by default the newest card first) */
  orderBy?: "committedAt" | null;
  page?: number;
  perPage?: number;
}

/** Drops `detail.message` from an error's body, after the default transforms have parsed it */
const dropErrorMessage: AxiosResponseTransformer = (data: unknown, _headers, status) => {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (!status || status < 400 || !detail || typeof detail !== "object" || !("message" in detail)) {
    return data;
  }
  const { message: _message, ...rest } = detail as Record<string, unknown>;
  return { ...(data as Record<string, unknown>), detail: rest };
};

/**
 * The axios options for `RecipeIngestRequestConfig`. `suppressAlert` covers a success's message; the interceptor's
 * error branch toasts any `detail.message` regardless, so a quiet request's error body loses its message instead (the
 * caller reads `detail.code`).
 */
export function requestOptions(config: RecipeIngestRequestConfig = {}): AxiosRequestConfig {
  if (!config.suppressAlert) {
    return {};
  }
  const defaults = axios.defaults.transformResponse;
  const transforms = Array.isArray(defaults) ? defaults : defaults ? [defaults] : [];
  return { suppressAlert: true, transformResponse: [...transforms, dropErrorMessage] };
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
      ...requestOptions(config),
    };
    return await this.requests.post<IngestResponse>(routes.ingest, buildIngestForm(files, options), requestConfig);
  }

  async createBatch(config: RecipeIngestRequestConfig = {}) {
    return await this.requests.post<RecipeIngestionBatchOut>(routes.batches, {}, requestOptions(config));
  }

  /** Marks a batch done; send it once every card of the batch has uploaded or failed for good */
  async sealBatch(id: string, config: RecipeIngestRequestConfig = {}) {
    return await this.requests.post<RecipeIngestionBatchOut>(routes.batchesIdSeal(id), {}, requestOptions(config));
  }

  /**
   * The capture page's heartbeat for its open batch, so a pause in a stack doesn't end the batch: a 409 whose
   * `detail.code` is `batch_sealed` means it ended anyway (the next card starts a new one)
   */
  async touchBatch(id: string, config: RecipeIngestRequestConfig = {}) {
    return await this.requests.post<RecipeIngestionBatchOut>(routes.batchesIdTouch(id), {}, requestOptions(config));
  }

  async getBatch(id: string) {
    return await this.requests.get<RecipeIngestionBatchOut>(routes.batchesId(id));
  }

  /**
   * Adds the listed cards of a batch as recipes, each only while it's ready at the draft version given and has
   * nothing to check; the others come back in `skipped` with the reason
   */
  async commitClean(batchId: string, payload: BulkCommitRequest) {
    return await this.requests.post<BulkCommitOut>(routes.batchesIdCommitClean(batchId), payload);
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
    if (query.committedSince) {
      params.committedSince = query.committedSince instanceof Date
        ? query.committedSince.toISOString()
        : query.committedSince;
    }
    if (query.orderBy) {
      params.orderBy = query.orderBy;
    }
    if (query.page) {
      params.page = query.page;
    }
    if (query.perPage) {
      params.perPage = query.perPage;
    }
    // the generator emits no type for a `PaginationBase` subclass: its JSON is upstream's pagination
    return await this.requests.get<PaginationData<RecipeIngestionJobSummary>>(route(routes.jobs, params));
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

  /** Reads a card that failed because it had to stay on this server again, with the group's cloud providers */
  async readWithCloud(id: string) {
    return await this.requests.post<RecipeIngestionJobState>(routes.jobsIdReadWithCloud(id), {});
  }

  /** Adds this card's photos to another card as its next pages; answers the other card's state (being read) */
  async merge(id: string, payload: MergeRequest) {
    return await this.requests.post<RecipeIngestionJobState>(routes.jobsIdMerge(id), payload);
  }

  /** Builds the recipe again from an edited transcription of the card */
  async rebuild(id: string, payload: RebuildRequest) {
    return await this.requests.post<RecipeIngestionJobState>(routes.jobsIdRebuild(id), payload);
  }

  /** Parses the given ingredient lines with the AI (`refs`: the lines' ids) */
  async parseLines(id: string, payload: ParseLinesRequest) {
    return await this.requests.post<RecipeIngestionJobState>(routes.jobsIdParseLines(id), payload);
  }

  /**
   * Where on the card a field's text is, to start a re-read selection there. A 404 means there's no hint: it's
   * never shown as an error.
   */
  async regionHint(id: string, target: ProposalTarget) {
    const params: Record<string, QueryValue> = { field: target.field };
    if (target.ref) {
      params.ref = target.ref;
    }
    return await this.requests.get<RegionHintOut>(
      route(routes.jobsIdRegionHint(id), params),
      undefined,
      requestOptions({ suppressAlert: true }),
    );
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

  /**
   * Back to review: deletes the recipe the card became and makes the card ready again. A 409 whose `detail.code` is
   * `recipe_edited` asks first: send `force` to delete the edited recipe anyway.
   */
  async uncommit(id: string, payload: UncommitRequest = {}) {
    return await this.requests.post<RecipeIngestionJobState>(routes.jobsIdUncommit(id), payload);
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

  /** Ticks or unticks "verified", or changes a case's tags or notes */
  async updateEvalCase(slug: string, payload: EvalCaseUpdate) {
    return await this.requests.put<EvalCaseSummary, EvalCaseUpdate>(routes.evalCasesSlug(slug), payload);
  }

  /**
   * A zip of the case's JSON and photos, as a Blob. Quiet: an error's body is a Blob too, so the caller says what
   * went wrong by the status.
   */
  async downloadEvalCase(slug: string) {
    return await this.requests.get<Blob>(routes.evalCasesSlugDownload(slug), undefined, {
      responseType: "blob",
      suppressAlert: true,
    });
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

  /** 204 when the notifier got it; 502 `notification_failed` when it didn't (Apprise or the service refused) */
  async testNotifierEvents(notifierId: string, config: RecipeIngestRequestConfig = {}) {
    return await this.requests.post<null>(routes.notifiersIdEventsTest(notifierId), {}, requestOptions(config));
  }

  async getAbout() {
    return await this.requests.get<IngestAbout>(routes.about);
  }
}
