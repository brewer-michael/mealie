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
  DEFAULT_INGEST_LIMITS,
  cardTitle,
  commitWarningText,
  errorCodeOf,
  errorMessageOf,
  errorStatusOf,
  flagText,
  formatIngestDate,
  ingestErrorText,
  nextLimitReset,
  progressText,
  rejectReasonText,
  resetRecipeIngestCounts,
  resetRecipeIngestSettings,
  serverDate,
  sourceFileName,
  useRecipeIngestCounts,
  useRecipeIngestSettings,
} from "../use-recipe-ingest";
import type { TranslateFn } from "../use-recipe-ingest";
import { CANNOT_SHRINK } from "~/composables/use-recipe-ingest-uploads";
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
    const quietTest = await client.testNotifierEvents("n1", { suppressAlert: true });
    expect(toasted).toEqual([]);
    // the caller still reads the code
    expect(errorCodeOf(quiet.error)).toBe("paused_for_restore");
    expect(errorStatusOf(quiet.error)).toBe(503);
    expect(errorCodeOf(quietBatch.error)).toBe("paused_for_restore");
    expect(errorCodeOf(quietTest.error)).toBe("paused_for_restore");

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

  test("batch, commit, merge, rebuild and eval routes send their bodies", async () => {
    const requests = fakeRequests();
    const client = new RecipeIngestAPI(requests as unknown as ApiRequestInstance);

    await client.touchBatch("b1", { suppressAlert: true });
    await client.commitClean("b1", { jobIds: ["j1", "j2"], draftVersions: { j1: 3, j2: 1 } });
    await client.readWithCloud("j1");
    await client.merge("j2", { intoJobId: "j1" });
    await client.rebuild("j1", { transcription: "Banana Mug Cake\n1 banana" });
    await client.parseLines("j1", { refs: ["i1", "i3"] });
    await client.uncommit("j1");
    await client.uncommit("j1", { force: true });
    await client.updateEvalCase("banana-mug-cake", { verified: true, tags: ["handwritten", "faded"] });
    await client.regionHint("j1", { field: "ingredients", ref: "i2" });
    await client.regionHint("j1", { field: "name", ref: null });

    const posts = requests.post.mock.calls.map(call => [call[0], call[1]]);
    expect(posts).toEqual([
      ["/api/ai/ingest/batches/b1/touch", {}],
      ["/api/ai/ingest/batches/b1/commit-clean", { jobIds: ["j1", "j2"], draftVersions: { j1: 3, j2: 1 } }],
      ["/api/ai/ingest/jobs/j1/read-with-cloud", {}],
      ["/api/ai/ingest/jobs/j2/merge", { intoJobId: "j1" }],
      ["/api/ai/ingest/jobs/j1/rebuild", { transcription: "Banana Mug Cake\n1 banana" }],
      ["/api/ai/ingest/jobs/j1/parse-lines", { refs: ["i1", "i3"] }],
      ["/api/ai/ingest/jobs/j1/uncommit", {}],
      ["/api/ai/ingest/jobs/j1/uncommit", { force: true }],
    ]);
    // the heartbeat is quiet: a sealed batch or a paused server isn't news to the person scanning
    expect(requests.post.mock.calls[0]![2]).toMatchObject({ suppressAlert: true });
    expect(requests.put.mock.calls).toEqual([
      ["/api/ai/ingest/eval-cases/banana-mug-cake", { verified: true, tags: ["handwritten", "faded"] }],
    ]);
    // a region hint's 404 means "no hint", never an error to show
    expect(requests.get.mock.calls.map(call => call[0])).toEqual([
      "/api/ai/ingest/jobs/j1/region-hint?field=ingredients&ref=i2",
      "/api/ai/ingest/jobs/j1/region-hint?field=name",
    ]);
    expect(requests.get.mock.calls[0]![2]).toMatchObject({ suppressAlert: true });

    // an eval case's zip comes as a Blob, quietly: the settings card says what went wrong
    await client.downloadEvalCase("banana-mug-cake");
    expect(requests.get.mock.calls[2]).toEqual([
      "/api/ai/ingest/eval-cases/banana-mug-cake/download",
      undefined,
      { responseType: "blob", suppressAlert: true },
    ]);
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

  test("committed cards can be listed by commit time since a date", async () => {
    const requests = fakeRequests();
    const client = new RecipeIngestAPI(requests as unknown as ApiRequestInstance);

    await client.getJobs({ status: "committed", committedSince: new Date("2026-09-27T10:00:00Z"), orderBy: "committedAt", perPage: 50 });
    await client.getJobs({ committedSince: "2026-09-27T10:00:00.000Z" });

    expect(requests.get.mock.calls[0]![0]).toBe(
      "/api/ai/ingest/jobs?status=committed&committedSince=2026-09-27T10%3A00%3A00.000Z&orderBy=committedAt&perPage=50",
    );
    expect(requests.get.mock.calls[1]![0]).toBe("/api/ai/ingest/jobs?committedSince=2026-09-27T10%3A00%3A00.000Z");
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

  test("a rejection names the limit the file went over: the server's, or its defaults before they load", () => {
    expect(rejectReasonText("too_large", t)).toBe("This file is larger than 30 MB.");
    expect(rejectReasonText("too_many_pages", t)).toBe("A card can have at most 4 pages.");
    expect(rejectReasonText("too_many_pixels", t)).toBe("This photo has more than 100 megapixels.");
    expect(rejectReasonText("too_many_pixels", t, { jpeg: true })).toBe("This photo has more than 260 megapixels.");

    const limits = { ...DEFAULT_INGEST_LIMITS, maxFileBytes: 20 * 1024 * 1024, maxPagesPerCard: 6, maxJpegPixels: 200_000_000 };
    expect(rejectReasonText("too_large", t, { limits })).toBe("This file is larger than 20 MB.");
    expect(rejectReasonText("too_many_pages", t, { limits })).toBe("A card can have at most 6 pages.");
    expect(rejectReasonText("too_many_pixels", t, { limits, jpeg: true })).toBe("This photo has more than 200 megapixels.");
    expect(rejectReasonText("pdf_not_supported", t, { limits })).toBe("This PDF can't be opened. It may need a password or be damaged.");
  });

  test("the error texts end with a period, and the ones a user can act on say what to do", () => {
    for (const code of [...INGEST_ERROR_CODES, ...INGEST_API_ERROR_CODES]) {
      expect(ingestErrorText(code, null, t), code).toMatch(/[.?!]$/);
    }
    expect(ingestErrorText("local_only_unavailable", null, t)).toContain("A group manager can add one");
    expect(ingestErrorText("limit_reached", null, t)).toContain("resets at the start of next month");
    expect(t("recipe-ingest.error.network")).toBe("The server couldn't be reached. Check your connection and try again.");
  });

  test("dates the server sends are UTC, with or without an offset; limits reset on the 1st of the next UTC month", () => {
    expect(serverDate("2026-11-01T00:00:00Z")?.toISOString()).toBe("2026-11-01T00:00:00.000Z");
    expect(serverDate("2026-11-01T00:00:00+00:00")?.toISOString()).toBe("2026-11-01T00:00:00.000Z");
    expect(serverDate("2026-11-01T00:00:00")?.toISOString()).toBe("2026-11-01T00:00:00.000Z");
    expect(serverDate("2026-11-01T02:00:00+02:00")?.toISOString()).toBe("2026-11-01T00:00:00.000Z");
    expect(serverDate(null)).toBeNull();
    expect(serverDate("soon")).toBeNull();

    expect(nextLimitReset(new Date("2026-10-04T15:00:00Z")).toISOString()).toBe("2026-11-01T00:00:00.000Z");
    expect(nextLimitReset(new Date("2026-12-31T23:59:59Z")).toISOString()).toBe("2027-01-01T00:00:00.000Z");
    expect(nextLimitReset(new Date("2026-10-01T00:00:00Z")).toISOString()).toBe("2026-11-01T00:00:00.000Z");

    const noon = new Date("2026-11-14T12:00:00Z");
    expect(formatIngestDate(noon, "en-US")).toBe("Nov 14, 2026");
    expect(formatIngestDate(noon, "en-US", true)).toBe(
      new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" }).format(noon),
    );
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

  test("every en-US recipe-ingest text is used by the app", () => {
    const app = resolve(__dirname, "../..");
    const code = (readdirSync(app, { recursive: true }) as string[])
      .filter(file => /\.(vue|ts)$/.test(file) && !/\.test\.ts$|__tests__|lib[\\/]api[\\/]types/.test(file))
      .map(file => readFileSync(join(app, file), "utf8"))
      .join("\n");
    // named in full
    const named = new Set([...code.matchAll(/["'`](recipe-ingest\.[\w.-]+[\w-])["'`]/g)].map(match => match[1]!));
    // the server's progress keys (`pipeline/context.py`), and the bare steps the app names (`progressText("queued")`)
    const backend = resolve(app, "../../mealie/services/ai/ingest");
    const python = (readdirSync(backend, { recursive: true }) as string[])
      .filter(file => file.endsWith(".py"))
      .map(file => readFileSync(join(backend, file), "utf8"))
      .join("\n");
    const steps = [
      ...[...python.matchAll(/\{PROGRESS_PREFIX\}([\w-]+)"/g)].map(match => match[1]!),
      ...[...code.matchAll(/progressText\("([\w-]+)"\)/g)].map(match => match[1]!),
    ];
    expect(steps).toContain("reading-card");
    // texts chosen by a code from the code lists: only the codes the app knows have one
    const byCode: [string, readonly string[]][] = [
      ["recipe-ingest.error.", [...INGEST_ERROR_CODES, ...INGEST_API_ERROR_CODES]],
      ["recipe-ingest.reject.", [...INGEST_REJECT_REASONS, CANNOT_SHRINK]],
      ["recipe-ingest.flag.", CARD_FLAG_KINDS],
      ["recipe-ingest.commit-warning.", COMMIT_WARNING_KINDS],
      ["recipe-ingest.progress.", steps],
    ];
    const chosenByCode = (key: string) => byCode.some(([prefix, codes]) => key.startsWith(prefix) && codes.some(
      known => [`${prefix}${known}`, `${prefix}${known}-detail`].includes(key) || key.startsWith(`${prefix}${known}.`),
    ));
    // other keys built in a template (`recipe-ingest.queue.source-${source}`): any key it can build
    const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const templates = [...code.matchAll(/`(recipe-ingest\.[^`]*?\$\{[^`]*)`/g)]
      .map(match => match[1]!)
      .filter(template => !byCode.some(([prefix]) => template.startsWith(prefix)))
      .map(template => new RegExp(`^${template.split(/\$\{[^}]*\}/).map(escape).join("[\\w-]+")}$`));

    const leaves = (node: unknown, prefix: string): string[] => typeof node === "object" && node
      ? Object.entries(node).flatMap(([key, value]) => leaves(value, `${prefix}.${key}`))
      : [prefix];
    const messages = (i18n.global.getLocaleMessage("en-US") as Record<string, unknown>)["recipe-ingest"];
    const unused = leaves(messages, "recipe-ingest")
      .filter(key => !named.has(key) && !chosenByCode(key) && !templates.some(template => template.test(key)));
    expect(unused).toEqual([]);
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

  test("a card's name in lists: its title, else its file (inbox, API), else its place in the batch", () => {
    const card = { title: null, source: "app", sourceName: "upload/IMG_1.jpg", status: "processing", position: 2 } as const;
    expect(cardTitle({ ...card, title: "Scones" }, t)).toBe("Scones");
    expect(cardTitle(card, t)).toBe("Card 3");
    expect(cardTitle({ ...card, source: "inbox", sourceName: "inbox/home/kitchen/scan.jpg" }, t)).toBe("scan.jpg");
    expect(cardTitle({ ...card, status: "ready" }, t)).toBe("Untitled card");
  });

  test("an error's message is the one the API client already showed", () => {
    expect(errorMessageOf({ response: { status: 503, data: { detail: { code: "paused_for_restore", message: "Paused" } } } }))
      .toBe("Paused");
    expect(errorMessageOf({ response: { status: 404, data: { detail: { code: "not_found" } } } })).toBeNull();
    expect(errorMessageOf({ response: { status: 403, data: { detail: "Forbidden" } } })).toBeNull();
    expect(errorMessageOf(new Error("network"))).toBeNull();
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
