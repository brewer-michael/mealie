<template>
  <!-- a page opened while a backup is restored: Mealie says so, with no error code, and opens once it's over -->
  <div v-if="restore" :class="['restore-page', scheme && `restore-page--${scheme}`]">
    <main class="restore-page__card" role="status" aria-live="polite">
      <svg class="restore-page__icon" viewBox="0 0 24 24" aria-hidden="true">
        <path :d="mdiSilverwareVariant" />
      </svg>
      <h1 class="restore-page__title">
        {{ restore.title }}
      </h1>
      <p class="restore-page__text">
        {{ restore.text }}
      </p>
      <div class="restore-page__waiting" aria-hidden="true" />
    </main>
  </div>
  <!-- every other error: Nuxt's own error page, as without this file -->
  <NuxtErrorPage v-else :error="error" />
</template>

<script setup lang="ts">
/**
 * The app's error page. Fork hook (docs/ai/PHASE2.md §3.9): a page opened or reloaded while a backup is restored can't
 * start the app (`plugins/app-info.client.ts`), so it says "A backup is being restored. This page will open when it's
 * done." as Mealie: no status code, the tab titled "Mealie", and the page opens by itself once the restore is over
 * (`restorePageError`). It's drawn without Vuetify or the app's plugins, which haven't run then. Any other error is
 * Nuxt's own error page, unchanged (upstream has no error page of its own; `layouts/error.vue` isn't one in Nuxt 4).
 */
import { mdiSilverwareVariant } from "@mdi/js";
import type { NuxtError } from "#app";
import NuxtErrorPage from "#app/components/nuxt-error-page.vue";
import { restorePageData } from "~/composables/use-recipe-ingest-restore";

const props = defineProps<{ error: NuxtError }>();

// read once, as Nuxt's own page does: the page doesn't change while it's shown
const restore = restorePageData(props.error);

/** Light or dark as the user chose it in this browser (`useDark`, `plugins/dark-mode.client.ts`); else the system's */
function storedScheme(): "light" | "dark" | null {
  try {
    const stored = localStorage.getItem("vueuse-color-scheme");
    return stored === "light" || stored === "dark" ? stored : null;
  }
  catch {
    return null;
  }
}

const scheme = restore ? storedScheme() : null;

if (restore) {
  useHead({ title: "Mealie" });
}
</script>

<style scoped>
.restore-page {
  /* Mealie's colours (the server's theme can't be asked for while it restores) */
  --restore-primary: #e58325;
  --restore-background: #f5f5f5;
  --restore-surface: #ffffff;
  --restore-text: rgba(0, 0, 0, 0.87);
  --restore-muted: rgba(0, 0, 0, 0.6);
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  overflow-y: auto;
  background: var(--restore-background);
  color: var(--restore-text);
  font-family:
    Roboto,
    system-ui,
    -apple-system,
    "Segoe UI",
    sans-serif;
}

@media (prefers-color-scheme: dark) {
  .restore-page:not(.restore-page--light) {
    --restore-background: #121212;
    --restore-surface: #1e1e1e;
    --restore-text: rgba(255, 255, 255, 0.87);
    --restore-muted: rgba(255, 255, 255, 0.6);
  }
}

.restore-page--dark {
  --restore-background: #121212;
  --restore-surface: #1e1e1e;
  --restore-text: rgba(255, 255, 255, 0.87);
  --restore-muted: rgba(255, 255, 255, 0.6);
}

.restore-page__card {
  width: 100%;
  max-width: 420px;
  padding: 32px 24px;
  border-radius: 8px;
  background: var(--restore-surface);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
  text-align: center;
}

.restore-page__icon {
  width: 64px;
  height: 64px;
  fill: var(--restore-primary);
}

.restore-page__title {
  margin: 16px 0 8px;
  font-size: 1.375rem;
  font-weight: 500;
  line-height: 1.3;
  overflow-wrap: anywhere;
}

.restore-page__text {
  margin: 0;
  font-size: 1rem;
  line-height: 1.5;
  color: var(--restore-muted);
  overflow-wrap: anywhere;
}

/* a quiet sign that the page is waiting */
.restore-page__waiting {
  position: relative;
  height: 4px;
  margin-top: 24px;
  overflow: hidden;
  border-radius: 2px;
  background: color-mix(in srgb, var(--restore-primary) 20%, transparent);
}

.restore-page__waiting::after {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 40%;
  border-radius: 2px;
  background: var(--restore-primary);
  animation: restore-page-waiting 1.6s ease-in-out infinite;
}

@keyframes restore-page-waiting {
  from {
    transform: translateX(-100%);
  }

  to {
    transform: translateX(250%);
  }
}

@media (prefers-reduced-motion: reduce) {
  .restore-page__waiting::after {
    width: 100%;
    animation: none;
    opacity: 0.6;
  }
}
</style>
