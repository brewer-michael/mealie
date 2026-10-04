/**
 * Recipe card ingestion (docs/ai/PHASE2.md): the group's card settings and the ready/processing counts that the
 * sidebar, the cards page and the settings card share, the layout's recipe card entries, and the text for error
 * codes, rejections, progress keys and flags. Fork-owned.
 */
import { useEventListener } from "@vueuse/core";
import type { Ref } from "vue";
import { useUserApi } from "~/composables/api";
import { useGlobalI18n } from "~/composables/use-global-i18n";
import { alert } from "~/composables/use-toast";
import type {
  CardFlag,
  CardFlagKind,
  IngestErrorCode,
  IngestLimits,
  IngestRejectReason,
  RecipeIngestionBatchOut,
  RecipeIngestionJobCounts,
  RecipeIngestionJobSummary,
  RecipeIngestionSettingsOut,
  RecipeIngestionSettingsUpdate,
} from "~/lib/api/types/recipe-ingest";

/** Translates a key with named values, like vue-i18n's `t` */
export type TranslateFn = (key: string, named?: Record<string, unknown>) => string;

export const INGEST_ERROR_CODES = [
  "ai_not_enabled",
  "local_only_unavailable",
  "limit_reached",
  "rate_limited",
  "provider_failed",
  "no_recipe_found",
  "files_missing",
  "owner_missing",
  "interrupted",
  "cancelled",
  "timeout",
  "internal_error",
  "commit_invalid",
  "commit_interrupted",
] as const satisfies readonly IngestErrorCode[];

/** Codes of API errors the review and cards pages read, beyond the job error codes */
export const INGEST_API_ERROR_CODES = [
  "version_conflict",
  "busy",
  "unresolved_flags",
  "paused_for_restore",
  "ingest_disabled",
  "too_many_jobs",
  "too_large",
  "unsupported_media_type",
  "authorization_required",
  "not_found",
  "invalid_status",
  "forbidden",
  "unknown_page",
  "unknown_target",
  "invalid_body",
  "nothing_accepted",
  "not_exportable",
  "eval_case_exists",
  "eval_case_error",
  "group_local_only",
  "too_many_pages",
  "same_card",
  "purged",
  "recipe_edited",
  "not_clean",
  "notification_failed",
  "user_quota",
] as const;

/** Kinds of the warnings a commit answers with (`tag_dropped:<name>`: an organizer that no longer exists) */
export const COMMIT_WARNING_KINDS = ["tag_dropped", "category_dropped", "tool_dropped"] as const;

export const CARD_FLAG_KINDS = [
  "illegible",
  "blank",
  "missing_name",
  "unsure",
  "not_on_card",
  "marker_dropped",
  "read_disagreement",
  "check_parse",
  "unit_unclear",
  "implausible_amount",
  "implausible_temperature",
  "empty_section",
  "read_by_ocr",
  "cross_read_failed",
  "shorthand_read",
  "not_parsed",
  "new_food",
  "new_unit",
  "linked_fuzzy",
  "organizers_skipped",
] as const satisfies readonly CardFlagKind[];

export const INGEST_REJECT_REASONS = [
  "too_large",
  "unsupported_format",
  "pdf_not_supported",
  "too_many_pixels",
  "unreadable_image",
  "too_many_pages",
  "duplicate",
  "url_not_allowed",
  "url_fetch_failed",
  "no_permission",
  "quota",
] as const satisfies readonly IngestRejectReason[];

const PROGRESS_PREFIX = "recipe-ingest.progress.";
const MIB = 1024 * 1024;

/** The server's limits (`limits.py`, `images.MAX_JPEG_SOURCE_PIXELS`), for texts shown before the settings load */
export const DEFAULT_INGEST_LIMITS: Pick<IngestLimits, "maxFileBytes" | "maxPagesPerCard" | "maxPixels" | "maxJpegPixels"> = {
  maxFileBytes: 30 * MIB,
  maxPagesPerCard: 4,
  maxPixels: 100_000_000,
  maxJpegPixels: 260_000_000,
};

function globalT(key: string, named?: Record<string, unknown>): string {
  return useGlobalI18n().t(key, named ?? {});
}

/** `t(key)`, or null when the key has no translation (vue-i18n answers with the key itself) */
function translated(t: TranslateFn, key: string, named?: Record<string, unknown>): string | null {
  const text = t(key, named);
  return text === key ? null : text;
}

/** The `detail.code` of a failed API call's response (`{"detail": {"code": ...}}`), if it has one */
export function errorCodeOf(error: unknown): string | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
  const code = (detail as { code?: unknown } | null | undefined)?.code;
  return typeof code === "string" ? code : null;
}

/**
 * The file a card came from, from its `sourceName` (`upload/<name>` for the app and the API,
 * `inbox/<group>/<household>/<name>` for the inbox); null without one
 */
export function sourceFileName(sourceName: string | null | undefined): string | null {
  if (!sourceName) {
    return null;
  }
  const parts = sourceName.split("/");
  const name = parts[0] === "upload" ? parts.slice(1) : parts[0] === "inbox" ? parts.slice(3) : parts;
  return name.join("/") || null;
}

/** What a card's name in a list depends on */
export type CardTitleFields = Pick<RecipeIngestionJobSummary, "title" | "source" | "sourceName" | "status" | "position">;

/**
 * A card's name in lists: its title; before it's read (or when it couldn't be), its file for inbox and API cards, else
 * its place in the batch
 */
export function cardTitle(job: CardTitleFields, t: TranslateFn = globalT): string {
  if (job.title) {
    return job.title;
  }
  const fileName = sourceFileName(job.sourceName);
  if (job.source !== "app" && fileName) {
    return fileName;
  }
  return job.status === "processing" || job.status === "failed"
    ? t("recipe-ingest.capture.card-number", { number: job.position + 1 })
    : t("recipe-ingest.queue.untitled");
}

/** The HTTP status of a failed API call, if it got a response */
export function errorStatusOf(error: unknown): number | null {
  return (error as { response?: { status?: number } } | null)?.response?.status ?? null;
}

/**
 * The `detail.message` of a failed API call's response: the API client has already shown it as a toast (requests
 * sent with `suppressAlert` lose it first), so the caller doesn't say it again
 */
export function errorMessageOf(error: unknown): string | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
  const message = (detail as { message?: unknown } | null | undefined)?.message;
  return typeof message === "string" && message ? message : null;
}

/**
 * The text for a job's error code or an API error code (`recipe-ingest.error.<code>`), with its params: a code with
 * a `detail` param uses the `<code>-detail` text when there is one. An unknown code gets a generic message naming it.
 */
export function ingestErrorText(
  code: string,
  params?: Record<string, unknown> | null,
  t: TranslateFn = globalT,
): string {
  const named = { ...params };
  if (named.detail) {
    const withDetail = translated(t, `recipe-ingest.error.${code}-detail`, named);
    if (withDetail) {
      return withDetail;
    }
  }
  return translated(t, `recipe-ingest.error.${code}`, named) ?? t("recipe-ingest.error.unknown", { code });
}

/** What a rejection's text says about the limit the file went over */
export interface RejectTextOptions {
  /** The server's limits (`settings.limits`); the defaults until they've loaded */
  limits?: Partial<IngestLimits> | null;
  /** The file is a JPEG, which may have more pixels (it's decoded at a reduced size) */
  jpeg?: boolean;
}

/** Why an uploaded photo wasn't used, with the limit it went over (`{mib}`, `{megapixels}`, `{pages}`) */
export function rejectReasonText(reason: string, t: TranslateFn = globalT, options: RejectTextOptions = {}): string {
  const limits = { ...DEFAULT_INGEST_LIMITS, ...options.limits };
  const named = {
    mib: Math.round(limits.maxFileBytes / MIB),
    megapixels: Math.round((options.jpeg ? limits.maxJpegPixels : limits.maxPixels) / 1_000_000),
    pages: limits.maxPagesPerCard,
  };
  return translated(t, `recipe-ingest.reject.${reason}`, named) ?? t("recipe-ingest.error.unknown", { code: reason });
}

/** A date-time the server sent: UTC, also when it has no offset; null for none, or one that isn't a date */
export function serverDate(value: string | Date | null | undefined): Date | null {
  if (!value) {
    return null;
  }
  const date = value instanceof Date
    ? value
    : new Date(/(Z|[+-]\d\d:?\d\d)$/i.test(value) || !value.includes("T") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** When monthly token limits next reset: the first instant of the next UTC month (`finalize.next_limit_reset`) */
export function nextLimitReset(now: Date = new Date()): Date {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
}

/** A date in the reader's locale and time zone ("Nov 1, 2026"), with the time when `withTime` ("…, 1:00 AM") */
export function formatIngestDate(date: Date, locale: string, withTime = false): string {
  return new Intl.DateTimeFormat(locale, withTime ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" })
    .format(date);
}

/**
 * A task's progress: the server stores full keys (`recipe-ingest.progress.reading-card`); a bare step name works too.
 * None without a key; the key itself when it has no translation.
 */
export function progressText(key: string | null | undefined, t: TranslateFn = globalT): string | null {
  if (!key) {
    return null;
  }
  const fullKey = key.startsWith(PROGRESS_PREFIX) ? key : PROGRESS_PREFIX + key;
  return translated(t, fullKey) ?? key;
}

/**
 * A commit warning's text (`recipe-ingest.commit-warning.<kind>`): the server sends `<kind>:<name>`. None for a kind
 * this page doesn't know.
 */
export function commitWarningText(warning: string, t: TranslateFn = globalT): string | null {
  const separator = warning.indexOf(":");
  const kind = separator < 0 ? warning : warning.slice(0, separator);
  const name = separator < 0 ? "" : warning.slice(separator + 1);
  return translated(t, `recipe-ingest.commit-warning.${kind}`, { name });
}

export interface FlagText {
  title: string;
  explanation: string;
  /** The one-tap resolution's label: "Keep as written" for errors that can be kept, "Looks right" for warnings */
  action: string | null;
}

function flagValues(flag: CardFlag): Record<string, unknown> {
  const params: Record<string, unknown> = { ...flag.params };
  const named: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    named[key] = Array.isArray(value) ? value.join(", ") : value;
  }
  if (typeof params.confidence === "number") {
    named.confidence = Math.round(params.confidence);
  }
  const alternatives = flag.alternatives?.length ? flag.alternatives : params.alternatives;
  named.alternatives = Array.isArray(alternatives) ? alternatives.map(a => `"${a}"`).join(", ") : alternatives ?? "";
  return named;
}

/** What a flag's text depends on beyond the flag itself */
export interface FlagTextContext {
  /** Whether the reviewer may create foods: a new food is otherwise kept as text at commit */
  canCreateFoods?: boolean;
}

function explanationKey(flag: CardFlag, context: FlagTextContext): string {
  const base = `recipe-ingest.flag.${flag.kind}`;
  const params = flag.params ?? {};
  switch (flag.kind) {
    case "blank":
      return flag.source === "cross_read" ? `${base}.explanation-cross-read` : `${base}.explanation`;
    case "read_disagreement":
      // Tesseract's check of a printed card's numbers, rather than a second reading by the AI provider
      return flag.source === "ocr" ? `${base}.explanation-ocr` : `${base}.explanation`;
    case "organizers_skipped":
      return params.reason === "local_only" || params.reason === "limit_reached"
        ? `${base}.explanation-${params.reason}`
        : `${base}.explanation`;
    case "empty_section":
      return params.section === "steps" ? `${base}.explanation-steps` : `${base}.explanation-ingredients`;
    case "implausible_amount":
      return params.suggestion ? `${base}.explanation` : `${base}.explanation-plain`;
    case "new_food": {
      // the server's flags don't depend on who reviews; commit keeps the name as text for a user who can't add foods
      const keptAsText = params.kept_as_text || params.keptAsText || context.canCreateFoods === false;
      return keptAsText ? `${base}.explanation-kept-as-text` : `${base}.explanation`;
    }
    default:
      return `${base}.explanation`;
  }
}

/** A flag's title, explanation (with its params) and resolution label */
export function flagText(flag: CardFlag, t: TranslateFn = globalT, context: FlagTextContext = {}): FlagText {
  const base = `recipe-ingest.flag.${flag.kind}`;
  const values = flagValues(flag);
  const title = translated(t, `${base}.title`) ?? flag.kind;
  const explanation = translated(t, explanationKey(flag, context), values) ?? "";

  let action: string | null = null;
  if (flag.severity !== "info") {
    action = translated(t, `${base}.action`)
      ?? (flag.severity === "warning" ? t("recipe-ingest.flag.actions.looks-right") : null);
  }
  return { title, explanation, action };
}

// ==========================================
// Composables

const sharedSettings = ref<RecipeIngestionSettingsOut | null>(null);
const settingsLoading = ref(false);
const settingsLoaded = ref(false);
const settingsLoadFailed = ref(false);
const settingsSaving = ref(false);
let settingsRequest: Promise<RecipeIngestionSettingsOut | null> | null = null;
/** Bumped by a reset (logout), so an answer still in flight changes nothing */
let settingsGeneration = 0;

/**
 * The group's recipe card settings: everyone reads them (the privacy chip, the layout's entries), group managers
 * change them. One module-level state, so a reload anywhere (the settings card after a provider changes) shows
 * everywhere; `load()` calls made while one is in flight share it.
 */
export function useRecipeIngestSettings() {
  const api = useUserApi();

  async function load(): Promise<RecipeIngestionSettingsOut | null> {
    if (!settingsRequest) {
      const gen = settingsGeneration;
      settingsRequest = (async () => {
        settingsLoading.value = true;
        try {
          const { data } = await api.recipeIngest.getSettings();
          if (gen !== settingsGeneration) {
            return null;
          }
          if (data) {
            sharedSettings.value = data;
            settingsLoaded.value = true;
          }
          settingsLoadFailed.value = !data;
          return data;
        }
        finally {
          if (gen === settingsGeneration) {
            settingsLoading.value = false;
            settingsRequest = null;
          }
        }
      })();
    }
    return await settingsRequest;
  }

  /** Saves the settings (managers); returns whether that worked. The saved settings replace the loaded ones. */
  async function save(update: RecipeIngestionSettingsUpdate) {
    settingsSaving.value = true;
    try {
      const { data } = await api.recipeIngest.updateSettings(update);
      if (data) {
        sharedSettings.value = data;
      }
      return !!data;
    }
    finally {
      settingsSaving.value = false;
    }
  }

  return {
    settings: sharedSettings,
    loading: readonly(settingsLoading),
    /** Whether the settings have loaded at least once */
    loaded: readonly(settingsLoaded),
    /** Whether the last load failed; the settings loaded before it are kept */
    loadFailed: readonly(settingsLoadFailed),
    saving: readonly(settingsSaving),
    load,
    save,
  };
}

/** Forgets the shared settings (on logout, and between tests) */
export function resetRecipeIngestSettings() {
  settingsGeneration += 1;
  sharedSettings.value = null;
  settingsLoading.value = false;
  settingsLoaded.value = false;
  settingsLoadFailed.value = false;
  settingsSaving.value = false;
  settingsRequest = null;
}

const sharedCounts = ref<RecipeIngestionJobCounts | null>(null);
let countsRequest: Promise<RecipeIngestionJobCounts | null> | null = null;
/** The layout refreshes the counts at most this often, on focus, on coming back to the tab and on route changes */
export const NAV_REFRESH_INTERVAL_MS = 30_000;
let lastNavRefresh = 0;

/**
 * The household's card counts, shared by every component (the sidebar's "Recipe cards (N)", the cards page): one
 * module-level ref, so an update anywhere shows everywhere. `refresh()` calls made while one is in flight share it.
 */
export function useRecipeIngestCounts() {
  const api = useUserApi();

  async function refresh(): Promise<RecipeIngestionJobCounts | null> {
    if (!countsRequest) {
      countsRequest = (async () => {
        try {
          const { data } = await api.recipeIngest.getCounts();
          if (data) {
            sharedCounts.value = data;
          }
          return data;
        }
        finally {
          countsRequest = null;
        }
      })();
    }
    return await countsRequest;
  }

  /** Sets the counts from an answer that carries them, without a request */
  function set(counts: RecipeIngestionJobCounts) {
    sharedCounts.value = counts;
  }

  return {
    counts: readonly(sharedCounts),
    /** Cards ready to review */
    ready: computed(() => sharedCounts.value?.ready ?? 0),
    refresh,
    set,
  };
}

/** Clears the shared counts (on logout, and between tests) */
export function resetRecipeIngestCounts() {
  sharedCounts.value = null;
  countsRequest = null;
  lastNavRefresh = 0;
}

// ==========================================
// The layout's recipe card entries

export interface RecipeIngestNavOptions {
  /** The signed-in user is in their own group, where the entries belong */
  active: Ref<boolean>;
  /** The current route: a change refreshes the counts and retries settings that failed to load */
  routePath: Ref<string>;
  /** Cards whose upload failed for good while no cards page was open (the upload queue's `failedWhileAway`) */
  failedWhileAway: Ref<number>;
  /** Opens the cards page (the failure toast's action) */
  openCards: () => void;
}

/** A small badge after a sidebar entry's title */
export interface RecipeIngestNavBadge {
  content: number;
  color: string;
  label: string;
}

/**
 * The default layout's recipe card entries (docs/ai/PHASE2.md §1.1): the sidebar's "Recipe cards (N)" and the Create
 * menu's "Scan recipe cards". Both follow the shared settings, which a group manager's changes reload; the counts are
 * refreshed on window focus, on coming back to the tab and on route changes, at most every 30 s, and settings that
 * failed to load are tried again on the next route change. The sidebar entry also shows while the household has cards
 * open (ready, failed or being read) even when new cards can't be read, and gets a red badge, with one toast, when an
 * upload failed for good while no cards page was open. Nothing is asked of a server with card scanning turned off.
 */
export function useRecipeIngestNav(options: RecipeIngestNavOptions) {
  const i18n = useI18n();
  const { settings, loaded, loading, loadFailed, load } = useRecipeIngestSettings();
  const counts = useRecipeIngestCounts();

  /** Whether the server takes cards (`AI_INGEST_ENABLED`): unknown counts as yes until the settings say otherwise */
  const serverTakesCards = computed(() => settings.value?.enabled !== false);
  const canReadCards = computed(() => options.active.value && !!settings.value?.canReadCards);
  const openCards = computed(() => {
    const current = counts.counts.value;
    return (current?.ready ?? 0) + (current?.failed ?? 0) + (current?.processing ?? 0);
  });

  function refreshCounts(force = false) {
    if (!options.active.value || !loaded.value || !serverTakesCards.value) {
      return;
    }
    const now = Date.now();
    if (!force && now - lastNavRefresh < NAV_REFRESH_INTERVAL_MS) {
      return;
    }
    lastNavRefresh = now;
    void counts.refresh();
  }

  async function loadSettings() {
    if (!loading.value) {
      await load();
    }
  }

  watch(options.active, (active) => {
    if (active && !loaded.value) {
      void loadSettings();
    }
  }, { immediate: true });
  // the settings loaded (here, or on a page that loaded them first): the counts follow
  watch(() => options.active.value && loaded.value && serverTakesCards.value, (allowed) => {
    if (allowed) {
      refreshCounts(true);
    }
  }, { immediate: true });
  watch(options.routePath, () => {
    if (options.active.value && loadFailed.value) {
      void loadSettings();
    }
    refreshCounts();
  });
  if (typeof window !== "undefined") {
    useEventListener(window, "focus", () => refreshCounts());
    useEventListener(document, "visibilitychange", () => {
      if (document.visibilityState === "visible") {
        refreshCounts();
      }
    });
  }

  watch(options.failedWhileAway, (count, before) => {
    if (count > (before ?? 0)) {
      alert.error(i18n.t("recipe-ingest.nav.upload-failed", count), null, {
        timeout: 10_000,
        action: { message: i18n.t("recipe-ingest.nav.open-recipe-cards"), onClick: options.openCards },
      });
    }
  });

  return {
    /** The sidebar's "Recipe cards (N)" */
    showCardsLink: computed(() => options.active.value && serverTakesCards.value
      && (canReadCards.value || openCards.value > 0 || options.failedWhileAway.value > 0)),
    /** The Create menu's "Scan recipe cards" (and the AI import page's link) */
    showScanLink: canReadCards,
    cardsTitle: computed(() => counts.ready.value
      ? i18n.t("recipe-ingest.nav.recipe-cards-count", { count: counts.ready.value })
      : i18n.t("recipe-ingest.nav.recipe-cards")),
    cardsBadge: computed<RecipeIngestNavBadge | null>(() => options.failedWhileAway.value > 0
      ? {
          content: options.failedWhileAway.value,
          color: "error",
          label: i18n.t("recipe-ingest.nav.upload-failed", options.failedWhileAway.value),
        }
      : null),
  };
}

// ==========================================
// Carried from one review page to the next (each card remounts the page)

/** How many batches `rememberRecipeIngestBatch` keeps */
const REMEMBERED_BATCHES = 20;
const rememberedBatches = new Map<string, RecipeIngestionBatchOut>();

/** The batch as last fetched, so the next card shows "Card 3 of 10" at once, before its own fetch answers */
export function rememberedRecipeIngestBatch(batchId: string): RecipeIngestionBatchOut | null {
  return rememberedBatches.get(batchId) ?? null;
}

export function rememberRecipeIngestBatch(batch: RecipeIngestionBatchOut) {
  rememberedBatches.delete(batch.id);
  rememberedBatches.set(batch.id, batch);
  if (rememberedBatches.size > REMEMBERED_BATCHES) {
    rememberedBatches.delete(rememberedBatches.keys().next().value as string);
  }
}

/** What Commit & next said: "Added Banana Mug Cake", and what the commit left out */
export interface RecipeIngestCommitNotice {
  text: string;
  warning: string | null;
  /** The card just added: the cards list offers Undo, which takes it back to review */
  undoJobId?: string | null;
}

/** A line in the cards list: what the review left for it, or what adding a batch's clean cards did */
export interface RecipeIngestQueueNotice {
  kind: "success" | "info" | "warning" | "error";
  text: string;
  detail: string | null;
  /** The cards it's about, one line each */
  items: string[];
  /** The batch it's about: shown in that batch's section while there is one, else at the top of the list */
  batchId?: string | null;
  /** The card it says was added: the line offers Undo */
  undoJobId?: string | null;
  /** A card the line is about: its "Open card" link */
  cardPath?: string | null;
}

/** A notice older than this is about an earlier visit (the next card's page didn't open) */
const COMMIT_NOTICE_MAX_AGE_MS = 10_000;
let commitNotice: { notice: RecipeIngestCommitNotice; at: number } | null = null;

/** Left for the next card's page, which shows it above its review bar (a toast would cover its header on phones) */
export function leaveRecipeIngestCommitNotice(notice: RecipeIngestCommitNotice) {
  commitNotice = { notice, at: Date.now() };
}

/** The notice the last commit left, once, while it's fresh */
export function takeRecipeIngestCommitNotice(): RecipeIngestCommitNotice | null {
  const left = commitNotice;
  commitNotice = null;
  return left && Date.now() - left.at <= COMMIT_NOTICE_MAX_AGE_MS ? left.notice : null;
}

/** Forgets what review pages carry over (on logout, and between tests) */
export function resetRecipeIngestReviewState() {
  rememberedBatches.clear();
  commitNotice = null;
}

// ==========================================
// The session, for what happens around a logout

/** How the default layout tells whether the session is still there; none until it says */
let sessionCheck: (() => boolean) | null = null;

/** The default layout says how to tell whether the user is still signed in (the in-memory token) */
export function setRecipeIngestSessionCheck(check: (() => boolean) | null) {
  sessionCheck = check;
}

/**
 * Whether the user is still signed in. A page asking before it's left ("photos haven't been uploaded", "changes not
 * saved") lets the page go when they aren't: an expired session's redirect to the login page clears the token first,
 * and nothing could be uploaded or saved without it.
 */
export function recipeIngestSignedIn(): boolean {
  return sessionCheck ? sessionCheck() : true;
}

/** What a logout the user chose waits for (briefly) before the session goes: a review page's edit not saved yet */
const logoutTasks = new Set<() => Promise<unknown>>();

/** Runs `task` before a logout the user chose clears the session; returns what to call when it's no longer needed */
export function onRecipeIngestLogout(task: () => Promise<unknown>): () => void {
  logoutTasks.add(task);
  return () => {
    logoutTasks.delete(task);
  };
}

/** Starts what a logout waits for; each settles, whether it worked or not (`prepareRecipeIngestLogout` times them) */
export function runRecipeIngestLogoutTasks(): Promise<unknown>[] {
  return [...logoutTasks].map(task => Promise.resolve().then(task).catch(error => console.error(error)));
}

/** The text helpers bound to the component's i18n */
export function useRecipeIngestText() {
  const i18n = useI18n();
  const t: TranslateFn = (key, named) => i18n.t(key, named ?? {});
  return {
    ingestErrorText: (code: string, params?: Record<string, unknown> | null) => ingestErrorText(code, params, t),
    rejectReasonText: (reason: string, options?: RejectTextOptions) => rejectReasonText(reason, t, options),
    progressText: (key: string | null | undefined) => progressText(key, t),
    flagText: (flag: CardFlag, context?: FlagTextContext) => flagText(flag, t, context),
    commitWarningText: (warning: string) => commitWarningText(warning, t),
    cardTitle: (job: CardTitleFields) => cardTitle(job, t),
    dateText: (date: Date, withTime = false) => formatIngestDate(date, i18n.locale.value, withTime),
  };
}
