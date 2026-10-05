<template>
  <v-container
    fluid
    class="d-flex justify-center align-center flex-column fill-height"
  >
    <v-card
      tag="section"
      class="w-100"
      max-width="560"
      :aria-busy="busy"
    >
      <v-toolbar color="primary" dark>
        <v-toolbar-title class="text-headline-small text-center">
          Mealie
        </v-toolbar-title>
      </v-toolbar>

      <!-- Inside another site's frame, a click on Approve could be a trick (clickjacking): only in its own tab -->
      <template v-if="framed">
        <v-card-text class="pt-6">
          <p class="text-body-large mb-0 consent-framed">
            {{ $t("mcp.consent.framed") }}
          </p>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn
            variant="elevated"
            color="primary"
            :href="route.fullPath"
            target="_blank"
            rel="noopener"
          >
            {{ $t("mcp.consent.open-in-new-tab") }}
          </v-btn>
        </v-card-actions>
      </template>

      <AppLoader v-else-if="state === 'loading' || !signedIn" />

      <template v-else-if="request && (state === 'ready' || busy)">
        <v-card-title tag="h1" class="text-title-large text-wrap pt-6 consent-title">
          <i18n-t keypath="mcp.consent.wants-access" tag="span">
            <template #client>
              <strong>{{ request.clientName }}</strong>
            </template>
          </i18n-t>
        </v-card-title>
        <v-card-text>
          <div class="mb-2">
            {{ $t("mcp.consent.it-will-be-able-to") }}
          </div>
          <ul class="ms-6 mb-2">
            <li>{{ $t("mcp.consent.read-access") }}</li>
          </ul>
          <!-- Only when the client asked for changes and may have them; the user opts in -->
          <v-checkbox
            v-if="request.writesOffered"
            v-model="allowWrites"
            :label="$t('mcp.consent.allow-changes')"
            :disabled="busy"
            hide-details
            density="compact"
            class="allow-changes"
          />
          <v-alert
            v-if="decisionFailed"
            type="error"
            density="compact"
            variant="tonal"
            class="mt-4"
          >
            {{ $t("mcp.consent.decision-failed") }}
          </v-alert>
          <div class="text-body-medium mt-4 mb-1 signed-in-as">
            {{ $t("mcp.consent.signed-in-as", { name: accountName }) }}
          </div>
          <div class="text-body-small text-medium-emphasis mb-1 redirect-notice">
            {{ $t("mcp.consent.redirect-notice", { host: request.redirectHost }) }}
          </div>
          <p class="text-body-small text-medium-emphasis mb-0">
            {{ $t("mcp.consent.only-approve") }}
          </p>
          <div v-if="state === 'redirecting'" class="text-body-medium mt-4 mb-0" role="status">
            {{ $t("mcp.consent.redirecting", { host: request.redirectHost }) }}
          </div>
        </v-card-text>
        <v-card-actions class="flex-wrap">
          <v-btn variant="text" size="small" :disabled="busy" @click="switchAccount">
            {{ $t("mcp.consent.switch-account") }}
          </v-btn>
          <v-spacer />
          <v-btn
            variant="outlined"
            :disabled="busy"
            :loading="state === 'deciding' && pendingApprove === false"
            @click="submit(false)"
          >
            {{ $t("mcp.consent.deny") }}
          </v-btn>
          <v-btn
            variant="elevated"
            color="primary"
            :disabled="busy"
            :loading="state === 'deciding' && pendingApprove === true"
            @click="submit(true)"
          >
            {{ $t("mcp.consent.approve") }}
          </v-btn>
        </v-card-actions>
      </template>

      <template v-else>
        <v-card-text class="pt-6">
          <v-alert
            :type="state === 'error' ? 'error' : 'warning'"
            variant="tonal"
            class="consent-problem"
          >
            {{ problemMessage }}
          </v-alert>
          <div v-if="accountName" class="text-body-medium mt-4 mb-0">
            {{ $t("mcp.consent.signed-in-as", { name: accountName }) }}
          </div>
        </v-card-text>
        <v-card-actions class="flex-wrap">
          <v-btn v-if="accountName" variant="text" size="small" @click="switchAccount">
            {{ $t("mcp.consent.switch-account") }}
          </v-btn>
          <v-spacer />
          <v-btn v-if="state === 'error'" variant="outlined" @click="loadRequest">
            {{ $t("mcp.try-again") }}
          </v-btn>
          <v-btn variant="elevated" color="primary" to="/">
            {{ $t("mcp.consent.go-to-mealie") }}
          </v-btn>
        </v-card-actions>
      </template>
    </v-card>
  </v-container>
</template>

<script setup lang="ts">
import { isFramed, useMcpConsent } from "~/composables/use-mcp";

/**
 * Where `/api/oauth/authorize` sends the browser (docs/ai/PHASE3.md §4): the user approves or denies an app's
 * request, then goes back to the app. Signed-out users log in first (password or OIDC) and come back here.
 */
definePageMeta({
  layout: "blank",
});

const i18n = useI18n();
const route = useRoute();
const auth = useAuthBackend();
const { state, request, decisionFailed, load, decide } = useMcpConsent();

useSeoMeta({
  title: i18n.t("mcp.consent.title"),
});

const allowWrites = ref(false);
const pendingApprove = ref<boolean | null>(null);
/** RFC 9700 §4.16: nothing here is loaded or answered inside a frame */
const framed = isFramed();
let leaving = false;

const signedIn = computed(() => auth.status.value === "authenticated");
const busy = computed(() => state.value === "deciding" || state.value === "redirecting");
const accountName = computed(() => {
  const user = auth.data.value;
  if (!user) {
    return "";
  }
  return user.fullName && user.email ? `${user.fullName} (${user.email})` : user.fullName || user.email || "";
});

const problemMessage = computed(() => {
  switch (state.value) {
    case "invalid":
      return i18n.t("mcp.consent.invalid");
    case "not-found":
      return i18n.t("mcp.consent.not-found");
    default:
      return i18n.t("mcp.consent.load-failed");
  }
});

/** The login page brings the user back here afterwards (also after OIDC) */
function loginPath(extra = "") {
  return `/login?${extra}redirect=${encodeURIComponent(route.fullPath)}`;
}

function loadRequest() {
  allowWrites.value = false;
  return load(route.query.request);
}

watch(
  () => auth.status.value,
  (status) => {
    if (leaving || framed) {
      return;
    }
    if (status === "unauthenticated") {
      leaving = true;
      navigateTo(loginPath(), { replace: true });
    }
    else if (status === "authenticated" && state.value === "loading") {
      loadRequest();
    }
  },
  { immediate: true },
);

async function submit(approve: boolean) {
  if (framed) {
    return;
  }

  pendingApprove.value = approve;
  const target = await decide(approve, allowWrites.value);
  if (target) {
    leaving = true;
    // The app's redirect URI, with a code or an error; replaced so Back doesn't return to an answered request
    await navigateTo(target, { external: true, replace: true });
  }
}

async function switchAccount() {
  leaving = true;
  // `direct=1` stops an automatic OIDC redirect from signing straight back in as the same user
  await auth.signOut(loginPath("direct=1&"));
}
</script>
