import axios, { AxiosError, type AxiosRequestConfig } from "axios";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import {
  CARD_FLAG_KINDS,
  COMMIT_WARNING_KINDS,
  INGEST_API_ERROR_CODES,
  INGEST_ERROR_CODES,
  INGEST_REJECT_REASONS,
  commitWarningText,
  errorCodeOf,
  errorStatusOf,
  flagText,
  ingestErrorText,
  progressText,
  rejectReasonText,
  resetRecipeIngestCounts,
  resetRecipeIngestSettings,
  sourceFileName,
  useRecipeIngestCounts,
  useRecipeIngestSettings,
} from "../use-recipe-ingest";
import type { TranslateFn } from "../use-recipe-ingest";
import { RecipeIngestAPI, buildIngestForm } from "~/lib/api/user/recipe-ingest";
import type { ApiRequestInstance } from "~/lib/api/types/non-generated";
import type { CardFlag, RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";
import { i18n } from "~/tests/setup";

const api = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
  getCounts: vi.fn(),
}));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));

const t: TranslateFn = (key, named) => i18n.global.t(key, named ?? {});

function flag(overrides: Partial<CardFlag> = {}): CardFlag {
  return {
    id: "blank:steps:s1",
    kind: "blank",
    severity: "error",
    source: "marker",
    field: "steps",
    ref: "s1",
    params: {},
    alternatives: [],
    resolution: null,
    ...overrides,
  };
}

function settings(overrides: Partial<RecipeIngestionSettingsOut> = {}): RecipeIngestionSettingsOut {
  return {
    localOnly: false,
    crossRead: false,
    canReadCards: true,
    ocrAvailable: false,
    reader: { name: "Claude Sonnet", local: false, viaOcr: false },
    localOnlyAvailable: false,
    localReadiness: null,
    limits: {
      maxUploadBytes: 104857600,
      maxFileBytes: 31457280,
      maxImagesPerRequest: 20,
      maxPagesPerCard: 4,
      maxPixels: 100000000,
      maxJpegPixels: 256000000,
    },
    inbox: { enabled: false, folder: null },
    ...overrides,
  };
}

function fakeRequests() {
  const ok = (data: unknown = {}) => Promise.resolve({ data, error: null, response: null });
  return {
    get: vi.fn((..._args: unknown[]) => ok()),
    post: vi.fn((..._args: unknown[]) => ok()),
    put: vi.fn((..._args: unknown[]) => ok()),
    patch: vi.fn((..._args: unknown[]) => ok()),
    delete: vi.fn((..._args: unknown[]) => ok()),
  };
}

describe("the recipe card API client", () => {
  test("a card's photos go as one multipart form with its options as text fields", () => {
    const front = new File(["front"], "IMG_0001.HEIC", { type: "image/heic" });
    const back = new Blob(["back"], { type: "image/jpeg" });
    const form = buildIngestForm([front, back], { batchId: "b1", position: 3, localOnly: true });

    const files = form.getAll("files") as File[];
    expect(files.map(file => file.name)).toEqual(["IMG_0001.HEIC", "photo-2.jpg"]);
    expect(form.get("batchId")).toBe("b1");
    expect(form.get("position")).toBe("3");
    expect(form.get("localOnly")).toBe("true");
    expect(form.has("split")).toBe(false);
    expect(form.has("allowDuplicate")).toBe(false);
  });

  test("position 0 is sent, and unset options are left out", () => {
    const form = buildIngestForm([new Blob(["x"])], { position: 0, batchId: null, split: true });
    expect(form.get("position")).toBe("0");
    expect(form.get("split")).toBe("true");
    expect(form.has("batchId")).toBe(false);
  });

  test("uploads pass the progress callback, abort signal and alert suppression to axios", async () => {
    const requests = fakeRequests();
    const client = new RecipeIngestAPI(requests as unknown as ApiRequestInstance);
    const onUploadProgress = vi.fn();
    const controller = new AbortController();

    await client.upload([new Blob(["x"])], { batchId: "new" }, { onUploadProgress, signal: controller.signal, suppressAlert: true });

    const [url, body, config] = requests.post.mock.calls[0] as [string, FormData, Record<string, unknown>];
    expect(url).toBe("/api/ai/ingest");
    expect(body).toBeInstanceOf(FormData);
    expect(body.get("batchId")).toBe("new");
    expect(config).toMatchObject({ onUploadProgress, signal: controller.signal, suppressAlert: true });
    expect(config.transformResponse).toBeInstanceOf(Array);
  });

  test("a quiet request's error reaches the interceptor without its message, so nothing toasts it", async () => {
    // an axios instance with the error branch of plugins/axios.ts, which toasts any detail.message
    const instance = axios.create();
    const toasted: string[] = [];
    instance.interceptors.response.use(response => response, (error) => {
      if (error?.response?.data?.detail?.message) {
        toasted.push(error.response.data.detail.message);
      }
      return Promise.reject(error);
    });
    instance.defaults.adapter = (config) => {
      const data = JSON.stringify({ detail: { code: "paused_for_restore", message: "Recipe cards are paused" } });
      const response = { data, status: 503, statusText: "Service Unavailable", headers: {}, config, request: {} };
      return Promise.reject(new AxiosError("Request failed", AxiosError.ERR_BAD_RESPONSE, config, {}, response));
    };
    const requests = {
      post: (url: string, data: unknown, config?: AxiosRequestConfig) =>
        instance.post(url, data, config).then(r => ({ data: r.data, error: null }), (e: unknown) => ({ data: null, error: e })),
    };
    const client = new RecipeIngestAPI(requests as unknown as ApiRequestInstance);

    const quiet = await client.upload([new Blob(["x"])], {}, { suppressAlert: true });
    const quietBatch = await client.createBatch({ suppressAlert: true });
    expect(toasted).toEqual([]);
    // the caller still reads the code
    expect(errorCodeOf(quiet.error)).toBe("paused_for_restore");
    expect(errorStatusOf(quiet.error)).toBe(503);
    expect(errorCodeOf(quietBatch.error)).toBe("paused_for_restore");

    await client.upload([new Blob(["x"])]);
    expect(toasted).toEqual(["Recipe cards are paused"]);
  });

  test("routes match the API", async () => {
    const requests = fakeRequests();
    const client = new RecipeIngestAPI(requests as unknown as ApiRequestInstance);

    await client.createBatch();
    await client.sealBatch("b1");
    await client.getBatch("b1");
    await client.getCounts();
    await client.getJob("j1");
    await client.getJobState("j1");
    await client.updateJob("j1", { draftVersion: 2, draft: {} });
    await client.reextract("j1");
    await client.reread("j1", { page: 0, x: 0.1, y: 0.2, width: 0.3, height: 0.1, target: { field: "steps", ref: "s1" } });
    await client.retry("j1");
    await client.cancel("j1");
    await client.rotatePage("j1", 1, { degrees: 90 });
    await client.commit("j1", { draftVersion: 2 });
    await client.discard("j1");
    await client.saveEvalCase("j1", { slug: "banana-mug-cake", verified: true });
    await client.getEvalCases();
    await client.deleteEvalCase("banana-mug-cake");
    await client.getSettings();
    await client.updateSettings({ localOnly: true, crossRead: false });
    await client.getNotifierEvents("n1");
    await client.updateNotifierEvents("n1", { recipeIngestionReady: true });
    await client.testNotifierEvents("n1");
    await client.getAbout();

    const calls = (mock: ReturnType<typeof vi.fn>) => mock.mock.calls.map(call => call[0]);
    expect(calls(requests.get)).toEqual([
      "/api/ai/ingest/batches/b1",
      "/api/ai/ingest/jobs/counts",
      "/api/ai/ingest/jobs/j1",
      "/api/ai/ingest/jobs/j1/state",
      "/api/ai/ingest/eval-cases",
      "/api/ai/ingest/settings",
      "/api/ai/notifiers/n1/events",
      "/api/ai/about",
    ]);
    expect(calls(requests.post)).toEqual([
      "/api/ai/ingest/batches",
      "/api/ai/ingest/batches/b1/seal",
      "/api/ai/ingest/jobs/j1/reextract",
      "/api/ai/ingest/jobs/j1/reread",
      "/api/ai/ingest/jobs/j1/retry",
      "/api/ai/ingest/jobs/j1/cancel",
      "/api/ai/ingest/jobs/j1/pages/1/rotate",
      "/api/ai/ingest/jobs/j1/commit",
      "/api/ai/ingest/jobs/j1/eval-case",
      "/api/ai/notifiers/n1/events/test",
    ]);
    expect(calls(requests.put)).toEqual([
      "/api/ai/ingest/jobs/j1",
      "/api/ai/ingest/settings",
      "/api/ai/notifiers/n1/events",
    ]);
    expect(calls(requests.delete)).toEqual([
      "/api/ai/ingest/jobs/j1",
      "/api/ai/ingest/eval-cases/banana-mug-cake",
    ]);
    expect(client.pageImageUrl("j1", 0, "thumb")).toBe("/api/ai/ingest/jobs/j1/pages/0/thumb");
  });

  test("job filters repeat the status parameter, as FastAPI reads a list", async () => {
    const requests = fakeRequests();
    const client = new RecipeIngestAPI(requests as unknown as ApiRequestInstance);

    await client.getJobs({ status: ["ready", "failed"], batchId: "b1", page: 2, perPage: 25 });
    await client.getJobs();

    expect(requests.get.mock.calls[0]![0]).toBe(
      "/api/ai/ingest/jobs?status=ready&status=failed&batchId=b1&page=2&perPage=25",
    );
    expect(requests.get.mock.calls[1]![0]).toBe("/api/ai/ingest/jobs");
  });
});

describe("text", () => {
  test("every job error code, API error code and rejection reason has its own text", () => {
    for (const code of [...INGEST_ERROR_CODES, ...INGEST_API_ERROR_CODES]) {
      expect(ingestErrorText(code, null, t), code).not.toContain("recipe-ingest");
      expect(ingestErrorText(code, null, t), code).not.toBe(t("recipe-ingest.error.unknown", { code }));
    }
    for (const reason of INGEST_REJECT_REASONS) {
      expect(rejectReasonText(reason, t), reason).not.toContain("recipe-ingest");
    }
  });

  test("an error's params fill its text, and an unknown code is named", () => {
    expect(ingestErrorText("provider_failed", { detail: "AuthenticationError (HTTP 401)" }, t)).toBe(
      "The AI provider couldn't read the card (AuthenticationError (HTTP 401)).",
    );
    expect(ingestErrorText("provider_failed", {}, t)).toBe("The AI provider couldn't read the card.");
    expect(ingestErrorText("brand_new_code", null, t)).toContain("brand_new_code");
  });

  test("every flag kind has a title and an explanation", () => {
    for (const kind of CARD_FLAG_KINDS) {
      const text = flagText(flag({ kind, severity: "warning" }), t);
      expect(text.title, kind).not.toBe(kind);
      expect(text.title, kind).not.toContain("recipe-ingest");
      expect(text.explanation, kind).not.toBe("");
      expect(text.explanation, kind).not.toContain("recipe-ingest");
    }
  });

  test("flag text depends on where the flag came from and what it carries", () => {
    expect(flagText(flag(), t)).toEqual({
      title: "Left blank on the card",
      explanation: "The card leaves a gap here. Fill it in or keep it blank.",
      action: "Keep blank",
    });
    expect(flagText(flag({ source: "cross_read", params: { value: "2" } }), t).explanation).toContain("\"2\"");
    const disagreement = { kind: "read_disagreement", severity: "warning", params: { text: "Bake at 350°" } } as const;
    expect(flagText(flag(disagreement), t).explanation).toBe(
      "A second reading of the card says \"Bake at 350°\" here.",
    );
    expect(flagText(flag({ ...disagreement, source: "ocr", params: { value: "375", read: "350" } }), t).explanation)
      .toBe("Text recognition read \"350\" here. Check the number against the card.");
    const skipped = { kind: "organizers_skipped", severity: "info" } as const;
    expect(flagText(flag({ ...skipped, params: { reason: "failed" } }), t).explanation).toMatch(/couldn't be suggested/);
    expect(flagText(flag({ ...skipped, params: { reason: "local_only" } }), t).explanation).toMatch(/on your network/);
    expect(flagText(flag({ ...skipped, params: { reason: "limit_reached" } }), t).explanation).toMatch(/monthly/);
    expect(flagText(flag({ kind: "linked_fuzzy", severity: "warning", params: { name: "red onion", kind: "food" } }), t))
      .toEqual({ title: "Check the link", explanation: "Linked to \"red onion\": check it's the same thing.", action: "Looks right" });
    expect(flagText(flag({ kind: "empty_section", severity: "warning", params: { section: "steps" } }), t).explanation)
      .toMatch(/^No steps/);
    expect(flagText(flag({ kind: "implausible_amount", severity: "warning", params: { suggestion: "1 1/2" } }), t)
      .explanation).toContain("1 1/2");
    expect(flagText(flag({ kind: "read_by_ocr", severity: "warning", params: { confidence: 48.6 } }), t).explanation)
      .toContain("49%");
    expect(flagText(flag({ kind: "unsure", severity: "warning", alternatives: ["1/2", "1/4"] }), t).explanation)
      .toContain("\"1/2\", \"1/4\"");
  });

  test("errors that can be kept, warnings and infos get the right action", () => {
    expect(flagText(flag({ kind: "illegible" }), t).action).toBe("Keep as written");
    expect(flagText(flag({ kind: "missing_name" }), t).action).toBeNull();
    expect(flagText(flag({ kind: "check_parse", severity: "warning" }), t).action).toBe("Looks right");
    expect(flagText(flag({ kind: "new_food", severity: "info", params: { name: "ghee" } }), t)).toEqual({
      title: "New food",
      explanation: "\"ghee\" isn't one of your foods yet. It's added when you commit the card.",
      action: null,
    });
  });

  test("a new food is kept as text for a reviewer who can't add foods, whatever the flag says", () => {
    const newFood = flag({ kind: "new_food", severity: "info", field: "ingredients", params: { name: "ghee" } });
    expect(flagText(newFood, t, { canCreateFoods: false }).explanation).toBe(
      "\"ghee\" isn't one of your foods, and you can't add foods, so it's kept in the note.",
    );
    expect(flagText(newFood, t, { canCreateFoods: true }).explanation).toContain("It's added when you commit the card.");
  });

  test("commit warnings name what was left out; an unknown kind has no text", () => {
    for (const kind of COMMIT_WARNING_KINDS) {
      expect(commitWarningText(`${kind}:Desserts`, t), kind).toContain("\"Desserts\"");
    }
    expect(commitWarningText("tag_dropped:Desserts", t)).toBe("The tag \"Desserts\" no longer exists, so it wasn't added.");
    expect(commitWarningText("something_new:x", t)).toBeNull();
  });

  test("every recipe-ingest key the app's code names has an en-US text", () => {
    const app = resolve(__dirname, "../..");
    const sources = (readdirSync(app, { recursive: true }) as string[])
      .filter(file => /\.(vue|ts)$/.test(file) && !/\.test\.ts$|lib[\\/]api[\\/]types/.test(file));
    const keys = new Set<string>();
    for (const file of sources) {
      for (const match of readFileSync(join(app, file), "utf8").matchAll(/["'`](recipe-ingest\.[\w.-]+[\w-])["'`]/g)) {
        keys.add(match[1]!);
      }
    }
    expect(keys.size).toBeGreaterThan(100);
    expect([...keys].filter(key => !i18n.global.te(key, "en-US"))).toEqual([]);
  });

  test("progress keys are translated whether full or bare", () => {
    expect(progressText("recipe-ingest.progress.reading-card", t)).toBe("Reading the card");
    expect(progressText("orienting", t)).toBe("Turning the card upright");
    expect(progressText(null, t)).toBeNull();
    expect(progressText("recipe-ingest.progress.something-new", t)).toBe("recipe-ingest.progress.something-new");
  });

  test("a card's file name, from where it came", () => {
    expect(sourceFileName("upload/IMG_1.jpg")).toBe("IMG_1.jpg");
    expect(sourceFileName("inbox/home/kitchen/scan 2.jpg")).toBe("scan 2.jpg");
    expect(sourceFileName("upload/")).toBeNull();
    expect(sourceFileName(null)).toBeNull();
  });

  test("error codes and statuses are read off failed API calls", () => {
    const error = { response: { status: 409, data: { detail: { code: "version_conflict", current: 4 } } } };
    expect(errorCodeOf(error)).toBe("version_conflict");
    expect(errorStatusOf(error)).toBe(409);
    expect(errorCodeOf({ response: { status: 422, data: { detail: [{ msg: "bad" }] } } })).toBeNull();
    expect(errorCodeOf(null)).toBeNull();
    expect(errorStatusOf(new Error("network"))).toBeNull();
  });
});

describe("useRecipeIngestSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestSettings();
  });

  test("is shared by every caller, and loads made while one is in flight share it", async () => {
    let answer: (value: unknown) => void = () => {};
    api.getSettings.mockReturnValueOnce(new Promise((resolve) => {
      answer = resolve;
    }));
    const layout = useRecipeIngestSettings();
    const settingsCard = useRecipeIngestSettings();

    const first = layout.load();
    const second = settingsCard.load();
    expect(layout.loading.value).toBe(true);
    answer({ data: settings({ canReadCards: false }) });
    await Promise.all([first, second]);
    expect(api.getSettings).toHaveBeenCalledOnce();
    expect(layout.settings.value?.canReadCards).toBe(false);

    api.getSettings.mockResolvedValueOnce({ data: settings({ canReadCards: true }) });
    await settingsCard.load();
    expect(layout.settings.value?.canReadCards).toBe(true);
  });

  test("a logout forgets them, and an answer still in flight changes nothing", async () => {
    let answer: (value: unknown) => void = () => {};
    api.getSettings.mockReturnValueOnce(new Promise((resolve) => {
      answer = resolve;
    }));
    const { settings: shared, loaded, load } = useRecipeIngestSettings();
    const loading = load();
    resetRecipeIngestSettings();
    answer({ data: settings() });
    await loading;
    expect(shared.value).toBeNull();
    expect(loaded.value).toBe(false);
  });

  test("loads the settings, and keeps them when a later load fails", async () => {
    api.getSettings.mockResolvedValueOnce({ data: settings() });
    const { settings: loadedSettings, loaded, loadFailed, load } = useRecipeIngestSettings();

    await load();
    expect(loadedSettings.value?.reader?.name).toBe("Claude Sonnet");
    expect(loaded.value).toBe(true);

    api.getSettings.mockResolvedValueOnce({ data: null });
    await load();
    expect(loadFailed.value).toBe(true);
    expect(loadedSettings.value?.reader?.name).toBe("Claude Sonnet");
  });

  test("saving replaces the settings with the saved ones", async () => {
    api.updateSettings.mockResolvedValueOnce({ data: settings({ localOnly: true, localOnlyAvailable: true }) });
    const { settings: saved, save } = useRecipeIngestSettings();

    expect(await save({ localOnly: true, crossRead: false })).toBe(true);
    expect(api.updateSettings).toHaveBeenCalledWith({ localOnly: true, crossRead: false });
    expect(saved.value?.localOnly).toBe(true);

    api.updateSettings.mockResolvedValueOnce({ data: null });
    expect(await save({ localOnly: false, crossRead: false })).toBe(false);
    expect(saved.value?.localOnly).toBe(true);
  });
});

describe("useRecipeIngestCounts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestCounts();
  });

  test("is shared by every caller", async () => {
    api.getCounts.mockResolvedValueOnce({ data: { processing: 1, ready: 3, needsAttention: 1, failed: 0 } });
    const sidebar = useRecipeIngestCounts();
    const page = useRecipeIngestCounts();

    await page.refresh();
    expect(sidebar.counts.value?.ready).toBe(3);
    expect(sidebar.ready.value).toBe(3);

    sidebar.set({ processing: 0, ready: 2, needsAttention: 0, failed: 0 });
    expect(page.ready.value).toBe(2);
  });

  test("refreshes made while one is in flight share its request", async () => {
    let answer: (value: unknown) => void = () => {};
    api.getCounts.mockReturnValueOnce(new Promise((resolve) => {
      answer = resolve;
    }));
    const { refresh, counts } = useRecipeIngestCounts();

    const first = refresh();
    const second = refresh();
    answer({ data: { processing: 0, ready: 5, needsAttention: 2, failed: 1 } });
    await Promise.all([first, second]);

    expect(api.getCounts).toHaveBeenCalledTimes(1);
    expect(counts.value?.needsAttention).toBe(2);

    api.getCounts.mockResolvedValueOnce({ data: null });
    await refresh();
    expect(api.getCounts).toHaveBeenCalledTimes(2);
    expect(counts.value?.ready).toBe(5);
  });
});
