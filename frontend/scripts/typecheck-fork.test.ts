import { describe, expect, test } from "vitest";
import { isForkFile, parseErrors } from "./typecheck-fork.mjs";

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
    ]) {
      expect(isForkFile(file), file).toBe(true);
    }
    for (const file of [
      "app/components/Domain/QueryFilterBuilder.vue",
      "app/components/Layout/DefaultLayout.vue",
      "app/pages/g/[groupSlug]/r/create/ai.vue",
      "app/pages/admin/backups.vue",
      "app/lib/api/user/recipes/recipe.ts",
    ]) {
      expect(isForkFile(file), file).toBe(false);
    }
  });
});
