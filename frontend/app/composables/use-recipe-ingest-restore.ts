/**
 * A page opened or reloaded while a backup is restored (docs/ai/PHASE2.md §3.9): the server answers every request with
 * 503 `paused_for_restore` and `Retry-After` for a moment, the app's first one too (`plugins/app-info.client.ts`), so
 * the app can't start. The page says so instead of Nuxt's "503 Internal Server Error", asks again after each
 * `Retry-After`, and loads the app once the restore is over. Fork-owned.
 */
import axios from "axios";

/** The code of the server's answer while a backup is restored */
export const PAUSED_FOR_RESTORE = "paused_for_restore";
/** How long to wait before asking again when the answer says nothing (no `Retry-After`) */
export const RESTORE_CHECK_DEFAULT_MS = 5000;
/** The longest wait between two checks, whatever `Retry-After` says */
export const RESTORE_CHECK_MAX_MS = 60_000;

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

/**
 * What the app's first request (`plugins/app-info.client.ts`) fails with: for the server's answer while a backup is
 * restored, an error Nuxt's error page shows as "A backup is being restored. This page will open when it's done.",
 * and the page is loaded again once the restore is over (`waitForRestoreEnd`); any other error as it is
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
  // Nuxt's error page shows the status, `statusMessage` as its title and `message` under it
  return Object.assign(new Error(t("recipe-ingest.restore.page-text")), {
    statusCode: 503,
    statusMessage: t("recipe-ingest.restore.page-title"),
    fatal: true,
  });
}
