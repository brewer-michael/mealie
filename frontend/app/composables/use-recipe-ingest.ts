/**
 * Recipe card ingestion (docs/ai/PHASE2.md): the group's card settings, the ready/processing counts the sidebar and the
 * cards page share, and the text for error codes, rejections, progress keys and flags. Fork-owned.
 */
import { useUserApi } from "~/composables/api";
import { useGlobalI18n } from "~/composables/use-global-i18n";
import type {
  CardFlag,
  CardFlagKind,
  IngestErrorCode,
  IngestRejectReason,
  RecipeIngestionJobCounts,
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
] as const;

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
] as const satisfies readonly CardFlagKind[];

export const INGEST_REJECT_REASONS = [
  "too_large",
  "unsupported_format",
  "pdf_not_supported",
  "too_many_pixels",
  "unreadable_image",
  "too_many_pages",
  "duplicate",
] as const satisfies readonly IngestRejectReason[];

const PROGRESS_PREFIX = "recipe-ingest.progress.";

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

/** The HTTP status of a failed API call, if it got a response */
export function errorStatusOf(error: unknown): number | null {
  return (error as { response?: { status?: number } } | null)?.response?.status ?? null;
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

/** Why an uploaded photo wasn't used */
export function rejectReasonText(reason: string, t: TranslateFn = globalT): string {
  return translated(t, `recipe-ingest.reject.${reason}`) ?? t("recipe-ingest.error.unknown", { code: reason });
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

function explanationKey(flag: CardFlag): string {
  const base = `recipe-ingest.flag.${flag.kind}`;
  const params = flag.params ?? {};
  switch (flag.kind) {
    case "blank":
      return flag.source === "cross_read" ? `${base}.explanation-cross-read` : `${base}.explanation`;
    case "empty_section":
      return params.section === "steps" ? `${base}.explanation-steps` : `${base}.explanation-ingredients`;
    case "implausible_amount":
      return params.suggestion ? `${base}.explanation` : `${base}.explanation-plain`;
    case "new_food":
      return params.kept_as_text || params.keptAsText ? `${base}.explanation-kept-as-text` : `${base}.explanation`;
    default:
      return `${base}.explanation`;
  }
}

/** A flag's title, explanation (with its params) and resolution label */
export function flagText(flag: CardFlag, t: TranslateFn = globalT): FlagText {
  const base = `recipe-ingest.flag.${flag.kind}`;
  const values = flagValues(flag);
  const title = translated(t, `${base}.title`) ?? flag.kind;
  const explanation = translated(t, explanationKey(flag), values) ?? "";

  let action: string | null = null;
  if (flag.severity !== "info") {
    action = translated(t, `${base}.action`)
      ?? (flag.severity === "warning" ? t("recipe-ingest.flag.actions.looks-right") : null);
  }
  return { title, explanation, action };
}

// ==========================================
// Composables

/** The group's recipe card settings: everyone reads them (the privacy chip), group managers change them */
export function useRecipeIngestSettings() {
  const api = useUserApi();
  const settings = ref<RecipeIngestionSettingsOut | null>(null);
  const loading = ref(false);
  /** Whether the settings have loaded at least once */
  const loaded = ref(false);
  /** Whether the last load failed; the settings loaded before it are kept */
  const loadFailed = ref(false);
  const saving = ref(false);

  async function load() {
    loading.value = true;
    try {
      const { data } = await api.recipeIngest.getSettings();
      if (data) {
        settings.value = data;
        loaded.value = true;
      }
      loadFailed.value = !data;
      return data;
    }
    finally {
      loading.value = false;
    }
  }

  /** Saves the settings (managers); returns whether that worked. The saved settings replace the loaded ones. */
  async function save(update: RecipeIngestionSettingsUpdate) {
    saving.value = true;
    try {
      const { data } = await api.recipeIngest.updateSettings(update);
      if (data) {
        settings.value = data;
      }
      return !!data;
    }
    finally {
      saving.value = false;
    }
  }

  return {
    settings,
    loading: readonly(loading),
    loaded: readonly(loaded),
    loadFailed: readonly(loadFailed),
    saving: readonly(saving),
    load,
    save,
  };
}

const sharedCounts = ref<RecipeIngestionJobCounts | null>(null);
let countsRequest: Promise<RecipeIngestionJobCounts | null> | null = null;

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
}

/** The text helpers bound to the component's i18n */
export function useRecipeIngestText() {
  const i18n = useI18n();
  const t: TranslateFn = (key, named) => i18n.t(key, named ?? {});
  return {
    ingestErrorText: (code: string, params?: Record<string, unknown> | null) => ingestErrorText(code, params, t),
    rejectReasonText: (reason: string) => rejectReasonText(reason, t),
    progressText: (key: string | null | undefined) => progressText(key, t),
    flagText: (flag: CardFlag) => flagText(flag, t),
  };
}
