/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type CardFlagKind =
  | "illegible"
  | "blank"
  | "missing_name"
  | "unsure"
  | "not_on_card"
  | "marker_dropped"
  | "read_disagreement"
  | "check_parse"
  | "unit_unclear"
  | "implausible_amount"
  | "implausible_temperature"
  | "empty_section"
  | "read_by_ocr"
  | "cross_read_failed"
  | "shorthand_read"
  | "not_parsed"
  | "new_food"
  | "new_unit"
  | "linked_fuzzy"
  | "organizers_skipped";
export type CardFlagSeverity = "error" | "warning" | "info";
export type CardFlagSource = "marker" | "model" | "validator" | "parser" | "ocr" | "cross_read";
export type FlagResolution = "kept" | "dismissed";
export type CardProposalKind = "region" | "full";
export type CardProposalOrigin = "reextract" | "rebuild";
export type IngestReadPath = "image" | "ocr";
export type EvalCaseTag = "handwritten" | "printed" | "faded";
export type InboxWaitingReason = "cannot_read" | "local_only_unavailable" | "quota";
export type IngestRejectReason =
  | "too_large"
  | "unsupported_format"
  | "pdf_not_supported"
  | "too_many_pixels"
  | "unreadable_image"
  | "too_many_pages"
  | "duplicate"
  | "url_not_allowed"
  | "url_fetch_failed"
  | "no_permission";
export type IngestStatus = "processing" | "ready" | "failed" | "committing" | "committed";
export type PageRotationSource = "none" | "ocr" | "user" | "model";
export type IngestSource = "app" | "api" | "inbox";
export type IngestErrorCode =
  | "ai_not_enabled"
  | "local_only_unavailable"
  | "limit_reached"
  | "rate_limited"
  | "provider_failed"
  | "no_recipe_found"
  | "files_missing"
  | "owner_missing"
  | "interrupted"
  | "cancelled"
  | "timeout"
  | "internal_error"
  | "commit_invalid"
  | "commit_interrupted";
export type IngestTaskKind = "extract" | "reread";
export type IngestTaskState = "queued" | "running";
export type IngestLimitedFeature = "suggestions" | "cross_read";
export type RegionHintSource = "ocr" | "position";

export interface AINotifierEventsOut {
  recipeIngestionReady?: boolean;
}
export interface AINotifierEventsUpdate {
  recipeIngestionReady?: boolean;
}
export interface BulkCommitOut {
  committed?: BulkCommitted[];
  skipped?: BulkCommitSkipped[];
}
export interface BulkCommitted {
  jobId: string;
  recipeId: string;
  slug: string;
}
export interface BulkCommitSkipped {
  jobId: string;
  code: string;
}
export interface BulkCommitRequest {
  jobIds: string[];
  draftVersions?: {
    [k: string]: number;
  };
}
export interface CardDraft {
  schemaVersion?: number;
  name?: string;
  description?: string;
  recipeYield?: string | null;
  recipeYieldQuantity?: number | null;
  recipeServings?: number | null;
  prepTime?: string | null;
  performTime?: string | null;
  totalTime?: string | null;
  attribution?: string | null;
  useCardAsCover?: boolean;
  attachCardPhoto?: boolean | null;
  ingredients?: CardDraftIngredient[];
  steps?: CardDraftStep[];
  notes?: CardDraftNote[];
  tags?: CardDraftRef[];
  categories?: CardDraftRef[];
  tools?: CardDraftRef[];
}
export interface CardDraftIngredient {
  referenceId?: string;
  title?: string | null;
  originalText?: string;
  quantity?: number | null;
  unit?: CardDraftRef | null;
  food?: CardDraftRef | null;
  note?: string;
  display?: string;
  parseConfidence?: number | null;
  extractedHash?: string | null;
}
export interface CardDraftRef {
  id?: string | null;
  name?: string;
}
export interface CardDraftStep {
  id?: string;
  title?: string | null;
  text?: string;
}
export interface CardDraftNote {
  id?: string;
  title?: string;
  text?: string;
}
export interface CardDraftSaved {
  draftVersion: number;
  flags?: CardFlag[];
  errorCount?: number;
  warningCount?: number;
  ingredients?: CardDraftIngredient[] | null;
  duplicateOf?: RecipeIngestionRecipeRef | null;
  duplicateJob?: RecipeIngestionJobRef | null;
  duplicateName?: string | null;
}
export interface CardFlag {
  id: string;
  kind: CardFlagKind;
  severity: CardFlagSeverity;
  source: CardFlagSource;
  field: string;
  ref?: string | null;
  params?: {
    [k: string]: unknown;
  };
  alternatives?: string[];
  resolution?: FlagResolution | null;
}
export interface RecipeIngestionRecipeRef {
  id: string;
  slug?: string | null;
  name?: string | null;
}
export interface RecipeIngestionJobRef {
  id: string;
  title?: string | null;
}
export interface CardDraftUpdate {
  draftVersion: number;
  draft: CardDraft;
  flagResolutions?: {
    [k: string]: FlagResolution | null;
  };
  resolvedProposalIds?: string[];
  clearError?: boolean;
}
export interface CardProposal {
  id?: string;
  kind: CardProposalKind;
  target?: ProposalTarget | null;
  text?: string | null;
  readable?: boolean;
  alternatives?: string[];
  viaOcr?: boolean;
  draft?: CardDraft | null;
  origin?: CardProposalOrigin;
  createdAt?: string;
}
export interface ProposalTarget {
  field: string;
  ref?: string | null;
}
export interface CardReadInfo {
  readPath?: IngestReadPath | null;
  provider?: string | null;
  model?: string | null;
  ocrConfidence?: number | null;
  crossRead?: boolean;
  crossReadFailed?: boolean;
}
export interface CommitOut {
  recipeId: string;
  slug: string;
  nextJobId?: string | null;
  warnings?: string[];
}
export interface CommitRequest {
  draftVersion: number;
  draft?: CardDraft | null;
}
export interface EvalCaseOut {
  slug: string;
  files?: string[];
}
export interface EvalCaseRequest {
  slug: string;
  verified?: boolean;
  tags?: EvalCaseTag[];
  notes?: string;
}
export interface EvalCaseSummary {
  slug: string;
  name?: string | null;
  pageCount?: number;
  verified?: boolean;
  tags?: string[];
  notes?: string;
  createdAt?: string | null;
}
export interface EvalCaseUpdate {
  verified?: boolean | null;
  tags?: EvalCaseTag[] | null;
  notes?: string | null;
}
export interface ExtractionCompilerError {
  compiler: string;
  error: string;
}
export interface ExtractionMeta {
  pipelineVersion?: number;
  readPath?: IngestReadPath | null;
  language?: string | null;
  attribution?: string | null;
  unsure?: ExtractionUnsure[];
  crossReadLines?: string[] | null;
  crossReadFailed?: boolean;
  ocrConfidence?: number | null;
  provider?: string | null;
  model?: string | null;
  stepOutcomes?: {
    [k: string]: string;
  };
  compilerErrors?: ExtractionCompilerError[];
  usage?: ExtractionUsage[];
}
export interface ExtractionUnsure {
  text: string;
  alternatives?: string[];
  reason?: string;
}
export interface ExtractionUsage {
  feature: string;
  slot: string;
  provider?: string | null;
  model?: string | null;
  requests?: number;
  failures?: number;
  promptTokens?: number;
  completionTokens?: number;
  latencyMs?: number;
}
export interface IngestAbout {
  version: string;
  features: IngestAboutFeatures;
}
export interface IngestAboutFeatures {
  ingest: IngestAboutFeature;
  mcp?: boolean;
}
export interface IngestAboutFeature {
  enabled: boolean;
  maxUploadBytes: number;
  maxImagesPerRequest: number;
  maxPagesPerCard: number;
  inbox: boolean;
  worker?: boolean;
}
export interface IngestInboxInfo {
  enabled?: boolean;
  folder?: string | null;
  waiting?: number;
  waitingReason?: InboxWaitingReason | null;
  rejections?: IngestInboxRejection[];
}
export interface IngestInboxRejection {
  name: string;
  reason?: IngestRejectReason | null;
  at?: string | null;
}
export interface IngestLimits {
  maxUploadBytes: number;
  maxFileBytes: number;
  maxImagesPerRequest: number;
  maxPagesPerCard: number;
  maxPixels: number;
  maxJpegPixels: number;
}
export interface IngestRejected {
  index: number;
  filename?: string | null;
  reason: IngestRejectReason;
  duplicateOf?: string | null;
}
export interface IngestResponse {
  batchId?: string | null;
  jobs?: IngestedJob[];
  rejected?: IngestRejected[];
  summary: string;
}
export interface IngestedJob {
  id: string;
  status: IngestStatus;
  pageCount: number;
  reviewPath: string;
}
export interface LocalReadiness {
  image?: string[];
  default?: string[];
  fast?: string[];
  notPrivate?: string[];
}
export interface MergeRequest {
  intoJobId: string;
}
export interface OCRLine {
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface PageMeta {
  index: number;
  width: number;
  height: number;
  viewWidth: number;
  viewHeight: number;
  rotation?: number;
  rotationSource?: PageRotationSource;
  oriented?: boolean;
  rawSha256: string;
  pageSha256: string;
  originalFilename?: string | null;
  format: string;
  rawBytes: number;
  ocr?: PageOCR | null;
}
export interface PageOCR {
  text?: string;
  confidence?: number;
  lines?: OCRLine[];
}
export interface PageOut {
  index: number;
  width: number;
  height: number;
  viewWidth: number;
  viewHeight: number;
  rotation: number;
  rotationSource: PageRotationSource;
  oriented: boolean;
  originalFilename?: string | null;
  pageUrl: string;
  viewUrl: string;
  thumbUrl: string;
}
export interface ParseLinesRequest {
  refs: string[];
}
export interface ReaderInfo {
  name: string;
  local: boolean;
  viaOcr?: boolean;
}
export interface RebuildRequest {
  transcription: string;
}
export interface RecipeIngestionBatchJob {
  id: string;
  position: number;
  status: IngestStatus;
  errorCount?: number;
  warningCount?: number;
}
export interface RecipeIngestionBatchOut {
  id: string;
  source: IngestSource;
  createdAt?: string | null;
  lastUploadAt?: string | null;
  sealedAt?: string | null;
  notifiedAt?: string | null;
  counts?: RecipeIngestionJobCounts;
  jobs?: RecipeIngestionBatchJob[];
}
export interface RecipeIngestionJobCounts {
  processing?: number;
  ready?: number;
  needsAttention?: number;
  failed?: number;
}
export interface RecipeIngestionJobError {
  code: IngestErrorCode;
  params?: {
    [k: string]: unknown;
  };
}
export interface RecipeIngestionJobOut {
  id: string;
  batchId: string;
  position: number;
  status: IngestStatus;
  source: IngestSource;
  sourceName?: string | null;
  title?: string | null;
  pageCount: number;
  thumbUrl?: string | null;
  draftVersion: number;
  errorCount?: number;
  warningCount?: number;
  task?: RecipeIngestionJobTask | null;
  error?: RecipeIngestionJobError | null;
  recipe?: RecipeIngestionRecipeRef | null;
  localOnly?: boolean;
  canDiscard?: boolean;
  createdAt?: string | null;
  committedAt?: string | null;
  autoRetryAt?: string | null;
  expiresAt?: string | null;
  pages?: PageOut[];
  transcription?: string | null;
  read?: CardReadInfo | null;
  draft?: CardDraft | null;
  flags?: CardFlag[];
  proposals?: CardProposal[];
  permissions?: RecipeIngestionJobPermissions;
  duplicateOf?: RecipeIngestionRecipeRef | null;
  duplicateJob?: RecipeIngestionJobRef | null;
  duplicateName?: string | null;
  householdRecipesPublic?: boolean;
  cardPhotoDefault?: boolean;
}
export interface RecipeIngestionJobTask {
  kind: IngestTaskKind;
  state: IngestTaskState;
  progressKey?: string | null;
  cancelRequested?: boolean;
}
export interface RecipeIngestionJobPermissions {
  canCreateFoods?: boolean;
  canCreateOrganizers?: boolean;
  canDiscard?: boolean;
  canExportEval?: boolean;
  canReadWithCloud?: boolean;
  canUncommit?: boolean;
  canMerge?: boolean;
}
export interface RecipeIngestionJobState {
  draftVersion: number;
  status: IngestStatus;
  task?: RecipeIngestionJobTask | null;
  proposalIds?: string[];
  error?: RecipeIngestionJobError | null;
}
export interface RecipeIngestionJobSummary {
  id: string;
  batchId: string;
  position: number;
  status: IngestStatus;
  source: IngestSource;
  sourceName?: string | null;
  title?: string | null;
  pageCount: number;
  thumbUrl?: string | null;
  draftVersion: number;
  errorCount?: number;
  warningCount?: number;
  task?: RecipeIngestionJobTask | null;
  error?: RecipeIngestionJobError | null;
  recipe?: RecipeIngestionRecipeRef | null;
  localOnly?: boolean;
  canDiscard?: boolean;
  createdAt?: string | null;
  committedAt?: string | null;
  autoRetryAt?: string | null;
  expiresAt?: string | null;
}
export interface RecipeIngestionSettingsOut {
  enabled?: boolean;
  localOnly?: boolean;
  crossRead?: boolean;
  canReadCards?: boolean;
  limitReached?: boolean;
  limitedFeatures?: IngestLimitedFeature[];
  baseUrlSet?: boolean;
  readerRunning?: boolean;
  ocrAvailable?: boolean;
  reader?: ReaderInfo | null;
  localOnlyAvailable?: boolean;
  localReadiness?: LocalReadiness | null;
  limits: IngestLimits;
  inbox?: IngestInboxInfo;
}
export interface RecipeIngestionSettingsUpdate {
  localOnly?: boolean;
  crossRead?: boolean;
}
export interface RegionHintOut {
  page: number;
  x: number;
  y: number;
  width: number;
  height: number;
  source: RegionHintSource;
}
export interface RereadRequest {
  page: number;
  x: number;
  y: number;
  width: number;
  height: number;
  target: ProposalTarget;
}
export interface RotateRequest {
  degrees: 90 | 180 | 270;
}
export interface UncommitRequest {
  force?: boolean;
}
export interface UnresolvedFlagsDetail {
  code?: "unresolved_flags";
  flags?: CardFlag[];
}
