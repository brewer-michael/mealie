import { resetGroupRecipeActions } from "~/composables/use-group-recipe-actions";
import { resetGroupSelf } from "~/composables/use-groups";
import { resetHouseholdSelf } from "~/composables/use-households";
import { resetUserSelfRatings } from "~/composables/use-users/user-ratings";
import { resetBackups } from "~/composables/use-backups";
import { resetRecipes } from "~/composables/recipes/use-recipes";
import { resetUserRegistrationForm } from "~/composables/use-users/user-registration-form";
import { resetRecipeIngestState } from "~/composables/use-recipe-ingest-uploads"; // fork: recipe cards (docs/ai/PHASE2.md)

export function clearComposableCaches() {
  resetGroupRecipeActions();
  resetGroupSelf();
  resetHouseholdSelf();
  resetUserSelfRatings();
  resetBackups();
  resetRecipes();
  resetUserRegistrationForm();
  resetRecipeIngestState(); // fork: the card upload queue and counts belong to the user logging out
}
