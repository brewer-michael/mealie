/**
 * A page opened or reloaded while a backup is restored (docs/ai/PHASE2.md §3.9): the server answers every request with
 * 503 `paused_for_restore` and `Retry-After` for a moment, the app's first one too (`plugins/app-info.client.ts`), so
 * the app can't start. The page says so as Mealie (`app/error.vue`), not as Nuxt's "503" error page, asks again every
 * few seconds, and loads the app once the restore is over. Fork-owned.
 */
import axios from "axios";

/** The code of the server's answer while a backup is restored */
export const PAUSED_FOR_RESTORE = "paused_for_restore";
/** How long to wait before asking again when the answer says nothing (no `Retry-After`) */
export const RESTORE_CHECK_DEFAULT_MS = 5000;
/**
 * The longest wait between two checks, whatever `Retry-After` says: a restore takes seconds, and its Retry-After is a
 * minute (the server's word for any client), so the page opens within a few seconds of the restore's end
 */
export const RESTORE_CHECK_MAX_MS = 5000;

interface ErrorAnswer {
  response?: { status?: number; headers?: Record<string, unknown>; data?: { detail?: { code?: unknown } } };
}

/**
 * How long to wait before asking again (`Retry-After`, ms) when `error` is the server's answer while a backup is
 * restored (an axios error); null for any other error
 */
export function restoreRetryAfterMs(error: unknown): number | null {
  const response = (error as ErrorAnswer | null)?.response;
  if (response?.status !== 503 || response.data?.detail?.code !== PAUSED_FOR_RESTORE) {
    return null;
  }
  const seconds = Number(response.headers?.["retry-after"]);
  return Number.isFinite(seconds) && seconds > 0
    ? Math.min(seconds * 1000, RESTORE_CHECK_MAX_MS)
    : RESTORE_CHECK_DEFAULT_MS;
}

/** Whether the server can't be reached at all (no answer): it may be restarting, so it's asked again */
function unanswered(error: unknown): boolean {
  return !(error as ErrorAnswer | null)?.response;
}

/**
 * Asks the server again after `firstDelayMs`, then after each answer's `Retry-After`, until the restore is over:
 * `ended` is called once the app's first request is answered (or refused for another reason, which the app then
 * shows). Returns what stops it.
 */
export function waitForRestoreEnd(
  firstDelayMs: number,
  ended: () => void,
  check: () => Promise<unknown> = () => axios.get("/api/app/about"),
): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  const later = (ms: number) => {
    timer = setTimeout(() => {
      timer = null;
      void ask();
    }, ms);
  };
  async function ask() {
    let next: number | null = null;
    try {
      await check();
    }
    catch (error) {
      next = restoreRetryAfterMs(error) ?? (unanswered(error) ? RESTORE_CHECK_DEFAULT_MS : null);
    }
    if (stopped) {
      return;
    }
    if (next === null) {
      ended();
    }
    else {
      later(next);
    }
  }
  later(firstDelayMs);
  return () => {
    stopped = true;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };
}

/** What the restore page (`app/error.vue`) shows, carried by the error `restorePageError` makes (Nuxt keeps `data`) */
export interface RestorePageData {
  code: typeof PAUSED_FOR_RESTORE;
  title: string;
  text: string;
}

/** The restore page's texts when `error` is the one `restorePageError` made; null for any other error */
export function restorePageData(error: unknown): RestorePageData | null {
  const data = (error as { data?: Partial<RestorePageData> } | null)?.data;
  return data?.code === PAUSED_FOR_RESTORE && typeof data.title === "string" && typeof data.text === "string"
    ? { code: PAUSED_FOR_RESTORE, title: data.title, text: data.text }
    : null;
}

/**
 * What the app's first request (`plugins/app-info.client.ts`) fails with: for the server's answer while a backup is
 * restored, an error the app's error page (`app/error.vue`) shows as "A backup is being restored. This page will open
 * when it's done.", and the page is loaded again once the restore is over (`waitForRestoreEnd`); any other error as
 * it is
 */
export function restorePageError(
  error: unknown,
  t: (key: string) => string,
  reload: () => void = () => window.location.reload(),
): unknown {
  const retryAfter = restoreRetryAfterMs(error);
  if (retryAfter === null) {
    return error;
  }
  waitForRestoreEnd(retryAfter, reload);
  const data: RestorePageData = {
    code: PAUSED_FOR_RESTORE,
    title: t("recipe-ingest.restore.page-title"),
    text: t("recipe-ingest.restore.page-text"),
  };
  // the texts go in `data` too: Nuxt keeps it as it is, while `statusMessage` loses what an HTTP status line can't hold
  return Object.assign(new Error(data.text), { statusCode: 503, statusMessage: data.title, data, fatal: true });
}
