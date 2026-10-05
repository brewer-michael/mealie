import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import { isForkFile, parseErrors } from "./typecheck-fork.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** Every file under `dir`, relative to `frontend/` */
function filesUnder(dir: string): string[] {
  return readdirSync(join(root, dir), { recursive: true, withFileTypes: true })
    .filter(entry => entry.isFile())
    .map(entry => relative(root, join(entry.parentPath, entry.name)).split("\\").join("/"));
}

describe("typecheck:fork", () => {
  test("reads vue-tsc's errors, with their continuation lines", () => {
    const output = [
      "app/components/Domain/Ingest/IngestCapture.vue(12,3): error TS2322: Type 'string' is not assignable to type 'number'.",
      "  Type 'string' is not assignable to type 'number'.",
      "app/components/Domain/QueryFilterBuilder.vue(175,53): error TS2345: Argument of type 'FieldValue' is not assignable.",
      "",
    ].join("\n");
    const errors = parseErrors(output);
    expect(errors.map(error => error.file)).toEqual([
      "app/components/Domain/Ingest/IngestCapture.vue",
      "app/components/Domain/QueryFilterBuilder.vue",
    ]);
    expect(errors[0]!.text).toContain("\n  Type 'string'");
  });

  test("gates the fork's files only", () => {
    for (const file of [
      "app/components/Domain/Ingest/IngestCapture.vue",
      "app/components/Domain/Group/GroupAIProviderDialog.vue",
      "app/components/Domain/Group/GroupRecipeCardSettings.test.ts",
      "app/components/Domain/Household/HouseholdNotifierAIEvents.vue",
      "app/composables/use-recipe-ingest-uploads.ts",
      "app/composables/__tests__/use-recipe-ingest-nav.test.ts",
      "app/lib/api/user/recipe-ingest.ts",
      "app/lib/api/admin/admin-ai-providers.ts",
      "app/lib/api/user/group-ai-providers.ts",
      "app/pages/g/[groupSlug]/recipes/cards/[jobId].vue",
      "app/components/Layout/DefaultLayout.test.ts",
      "app/pages/admin/backups.test.ts",
      "app/plugins/__tests__/axios-restore-pause.test.ts",
      "app/composables/__tests__/use-auth-backend-restore-pause.test.ts",
      "app/pages/login.test.ts",
      "app/error.vue",
      "app/error.test.ts",
    ]) {
      expect(isForkFile(file), file).toBe(true);
    }
    for (const file of [
      "app/components/Domain/QueryFilterBuilder.vue",
      "app/components/Layout/DefaultLayout.vue",
      "app/pages/g/[groupSlug]/r/create/ai.vue",
      "app/pages/admin/backups.vue",
      "app/lib/api/user/recipes/recipe.ts",
      "app/plugins/axios.ts",
      "app/composables/use-auth-backend.ts",
      "app/pages/login.vue",
    ]) {
      expect(isForkFile(file), file).toBe(false);
    }
  });

  test("the fork's templates use Vuetify 4's type scale: Vuetify 3's classes no longer exist and do nothing", () => {
    // Vuetify 4 kept only the Material 3 names (text-body-small, text-title-medium, ...); see its styles/main.css
    const vuetify3 = /\btext-(caption|overline|body-[12]|subtitle-[12]|h[1-6])\b/g;
    const found = filesUnder("app")
      .filter(file => file.endsWith(".vue") && isForkFile(file))
      .flatMap(file => [...readFileSync(join(root, file), "utf8").matchAll(vuetify3)].map(match => `${file}: ${match[0]}`));
    expect(found).toEqual([]);
  });
});
