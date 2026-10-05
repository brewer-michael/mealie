import axios from "axios";
import type { AppInfo } from "~/lib/api/types/admin";
import { restorePageError } from "~/composables/use-recipe-ingest-restore";

export default defineNuxtPlugin({
  async setup(nuxtApp) {
    const { data } = await axios.get<AppInfo>("/api/app/about")
      // fork hook (docs/ai/PHASE2.md §3.9): opened while a backup is restored, the page says so and opens once it's over
      .catch((error: unknown): Promise<never> => Promise.reject(restorePageError(error, key => nuxtApp.$i18n.t(key))));

    return {
      provide: {
        appInfo: data,
      },
    };
  },
});
