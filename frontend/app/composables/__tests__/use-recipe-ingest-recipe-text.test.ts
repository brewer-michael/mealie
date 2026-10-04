/**
 * How a committed recipe card line reads on the recipe page, by upstream's own renderer (`useIngredientTextParser`):
 * it shows a unit only with an amount, so commit names the unit of a line whose amount is a marker kept as written
 * ("[blank] C. sugar") in its note, after the marker (`review.amount_marker_note`).
 */
import { beforeEach, describe, expect, test, vi } from "vitest";
import type { RecipeIngredient } from "~/lib/api/types/recipe";
import { useIngredientTextParser } from "~/composables/recipes/use-recipe-ingredients";
import { useLocales } from "~/composables/use-locales";

vi.mock("~/composables/use-locales");

const sugar = { id: "1", name: "sugar" };
const cup = { id: "2", name: "cup" };

describe("a recipe card line kept with a blank amount, on the recipe page", () => {
  beforeEach(() => {
    vi.mocked(useLocales).mockReturnValue({
      locales: [{ value: "en-US", pluralFoodHandling: "without-unit" }],
      locale: { value: "en-US" },
    } as unknown as ReturnType<typeof useLocales>);
  });

  test("reads its unit after the blank, as commit writes it", () => {
    const { parseIngredientText } = useIngredientTextParser();
    const committed: RecipeIngredient = { referenceId: "a", quantity: null, unit: null, food: sugar, note: "___ cup" };
    expect(parseIngredientText(committed, 1, false)).toBe("sugar ___ cup");
  });

  test("would lose its unit as a unit field without an amount", () => {
    const { parseIngredientText } = useIngredientTextParser();
    const unitField: RecipeIngredient = { referenceId: "a", quantity: null, unit: cup, food: sugar, note: "___" };
    expect(parseIngredientText(unitField, 1, false)).toBe("sugar ___");
  });
});
