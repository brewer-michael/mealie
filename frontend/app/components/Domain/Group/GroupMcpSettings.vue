<template>
  <div>
    <BaseCardSectionTitle :title="$t('mcp.ai-assistants')" />
    <v-card-text class="pt-0 pb-6 px-0">
      {{ $t("mcp.ai-assistants-description") }}
    </v-card-text>

    <v-text-field
      :model-value="serverUrl"
      :label="$t('mcp.server-url')"
      :hint="$t('mcp.server-url-hint')"
      persistent-hint
      readonly
      density="compact"
      variant="outlined"
      class="mb-2 server-url"
    >
      <template #append-inner>
        <v-btn
          :icon="$globals.icons.contentCopy"
          :aria-label="$t('mcp.copy-server-url')"
          variant="text"
          size="small"
          @click="copyText(serverUrl)"
        />
      </template>
    </v-text-field>
    <!-- API tokens are behind the profile's "Show advanced features" -->
    <i18n-t keypath="mcp.api-token-note" tag="p" class="text-body-small text-medium-emphasis mb-6 api-token-note">
      <template #tokens>
        <nuxt-link to="/user/profile/api-tokens">{{ $t("mcp.api-token-note-tokens") }}</nuxt-link>
      </template>
      <template #settings>
        <nuxt-link to="/user/profile/edit">{{ $t("mcp.api-token-note-settings") }}</nuxt-link>
      </template>
    </i18n-t>

    <!-- The client secret is only in the create and rotate responses: shown here once, until dismissed -->
    <v-card
      v-if="newSecret"
      variant="outlined"
      class="mb-6 new-secret"
    >
      <v-card-title tag="h4" class="text-title-medium text-wrap">
        {{ $t("mcp.new-secret-title", { name: newSecret.clientName }) }}
      </v-card-title>
      <v-card-text>
        <!-- Only the warning is in the alert; its colours are too faint for the rest (and the fields' labels) -->
        <v-alert
          type="warning"
          variant="tonal"
          density="compact"
          class="mb-6 new-secret-warning"
        >
          <span class="text-high-emphasis font-weight-bold">{{ $t("mcp.new-secret-warning") }}</span>
        </v-alert>
        <v-text-field
          :model-value="newSecret.clientId"
          :label="$t('mcp.client-id')"
          readonly
          density="compact"
          variant="outlined"
          class="mb-2 secret-field"
        >
          <template #append-inner>
            <v-btn
              :icon="$globals.icons.contentCopy"
              :aria-label="$t('mcp.copy-client-id')"
              variant="text"
              size="small"
              @click="copyText(newSecret.clientId)"
            />
          </template>
        </v-text-field>
        <v-text-field
          :model-value="newSecret.clientSecret"
          :label="$t('mcp.client-secret')"
          readonly
          density="compact"
          variant="outlined"
          class="mb-2 secret-field"
        >
          <template #append-inner>
            <v-btn
              :icon="$globals.icons.contentCopy"
              :aria-label="$t('mcp.copy-client-secret')"
              variant="text"
              size="small"
              @click="copyText(newSecret.clientSecret)"
            />
          </template>
        </v-text-field>
        <p class="text-body-medium mb-0">
          {{ $t("mcp.new-secret-instructions") }}
        </p>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="elevated" color="primary" @click="newSecret = null">
          {{ $t("general.done") }}
        </v-btn>
      </v-card-actions>
    </v-card>

    <BaseCardSectionTitle :title="$t('mcp.oauth-clients')" size="medium">
      <template #append-title>
        <BaseButton
          :text="$t('mcp.add-client')"
          :disabled="!!newSecret"
          class="ms-auto my-2"
          create
          small
          @click="openCreate"
        />
      </template>
    </BaseCardSectionTitle>
    <div class="text-body-medium mb-4">
      {{ $t("mcp.oauth-clients-description") }}
    </div>
    <!-- Another secret would replace the one shown before it's been copied -->
    <div v-if="newSecret" class="text-body-medium font-weight-medium mt-2 mb-4 secret-pending">
      {{ $t("mcp.secret-pending") }}
    </div>

    <!-- A failed refresh keeps showing the clients loaded before it -->
    <v-alert
      v-if="loadFailed"
      type="error"
      density="compact"
      variant="tonal"
      class="mb-4"
    >
      <div class="d-flex align-center flex-wrap">
        <span class="me-2">{{ $t("mcp.clients-load-failed") }}</span>
        <v-btn class="ms-auto" variant="text" size="small" :loading="loading" @click="load">
          {{ $t("mcp.try-again") }}
        </v-btn>
      </div>
    </v-alert>
    <AppLoader v-if="!loaded && loading" />
    <p v-else-if="loaded && !clients.length" class="text-body-medium text-medium-emphasis no-clients">
      {{ $t("mcp.no-clients") }}
    </p>

    <v-card
      v-for="client in clients"
      :key="client.id"
      variant="tonal"
      class="mb-4 mcp-client"
    >
      <v-card-item>
        <v-card-title tag="h4" class="text-title-medium">
          {{ client.name }}
        </v-card-title>
        <v-card-subtitle class="text-wrap">
          {{ client.isConfidential ? $t("mcp.confidential-client") : $t("mcp.public-client") }}
          ·
          {{ client.allowWriteScope ? $t("mcp.changes-allowed") : $t("mcp.read-only") }}
        </v-card-subtitle>
      </v-card-item>
      <v-card-text class="pb-0">
        <div class="d-flex align-center flex-wrap">
          <span class="text-medium-emphasis me-2">{{ $t("mcp.client-id") }}:</span>
          <code class="client-id">{{ client.clientId }}</code>
          <v-btn
            :icon="$globals.icons.contentCopy"
            :aria-label="$t('mcp.copy-client-id')"
            variant="text"
            size="x-small"
            class="ms-1"
            @click="copyText(client.clientId)"
          />
        </div>
        <div class="mt-2">
          <span class="text-medium-emphasis">{{ $t("mcp.redirect-uris") }}:</span>
          <ul class="redirect-uris ms-6">
            <li v-for="uri in client.redirectUris" :key="uri">
              <code>{{ uri }}</code>
            </li>
          </ul>
        </div>
        <div class="mt-2 text-medium-emphasis last-used">
          {{ client.lastUsedAt
            ? $t("mcp.last-used", { date: formatDateTime(client.lastUsedAt, locale) })
            : $t("mcp.never-used") }}
        </div>
      </v-card-text>
      <v-card-actions class="flex-wrap">
        <v-btn variant="text" size="small" :prepend-icon="$globals.icons.edit" @click="openEdit(client)">
          {{ $t("general.edit") }}
        </v-btn>
        <v-btn
          v-if="client.isConfidential"
          variant="text"
          size="small"
          :disabled="!!newSecret"
          :prepend-icon="$globals.icons.refresh"
          @click="openConfirm('rotate', client)"
        >
          {{ $t("mcp.rotate-secret") }}
        </v-btn>
        <v-btn
          variant="text"
          size="small"
          color="error"
          :prepend-icon="$globals.icons.delete"
          @click="openConfirm('delete', client)"
        >
          {{ $t("general.delete") }}
        </v-btn>
      </v-card-actions>
    </v-card>

    <GroupMcpClientDialog
      v-model="dialogOpen"
      :client="editingClient"
      @created="handleCreated"
      @updated="handleUpdated"
    />

    <BaseDialog
      v-model="rotateDialogOpen"
      bottom-sheet
      :title="$t('mcp.rotate-secret')"
      color="warning"
      :icon="$globals.icons.alertCircle"
      can-confirm
      @confirm="handleRotate"
    >
      <v-card-text>
        {{ $t("mcp.rotate-secret-confirm", { name: confirmTarget?.name ?? "" }) }}
      </v-card-text>
    </BaseDialog>
    <BaseDialog
      v-model="deleteDialogOpen"
      bottom-sheet
      :title="$t('mcp.delete-client')"
      color="error"
      :icon="$globals.icons.alertCircle"
      can-confirm
      @confirm="handleDelete"
    >
      <v-card-text>
        {{ $t("mcp.delete-client-confirm", { name: confirmTarget?.name ?? "" }) }}
      </v-card-text>
    </BaseDialog>
  </div>
</template>

<script setup lang="ts">
import GroupMcpClientDialog from "./GroupMcpClientDialog.vue";
import { formatDateTime, mcpServerUrl, useMcpClients } from "~/composables/use-mcp";
import { useCopy } from "~/composables/use-copy";
import { alert } from "~/composables/use-toast";
import type { McpClientCreated, McpClientOut } from "~/lib/api/types/mcp";

/** Group Settings → AI Assistants (MCP): the MCP server's URL and the group's OAuth clients (managers only) */
const i18n = useI18n();
const { copyText } = useCopy();
const { clients, loading, loaded, loadFailed, load, remove, rotateSecret } = useMcpClients();

const locale = computed(() => i18n.locale.value);
const serverUrl = mcpServerUrl(window.location.origin);

/** A secret just created or rotated, until it's dismissed */
const newSecret = ref<{ clientName: string; clientId: string; clientSecret: string } | null>(null);

const dialogOpen = ref(false);
const editingClient = ref<McpClientOut | null>(null);

const rotateDialogOpen = ref(false);
const deleteDialogOpen = ref(false);
const confirmTarget = ref<McpClientOut | null>(null);

onMounted(load);

function openCreate() {
  editingClient.value = null;
  dialogOpen.value = true;
}

function openEdit(client: McpClientOut) {
  editingClient.value = client;
  dialogOpen.value = true;
}

function openConfirm(action: "rotate" | "delete", client: McpClientOut) {
  confirmTarget.value = client;
  rotateDialogOpen.value = action === "rotate";
  deleteDialogOpen.value = action === "delete";
}

async function handleCreated(client: McpClientCreated) {
  // A public client has no secret; one still shown for another client stays
  if (client.clientSecret) {
    newSecret.value = { clientName: client.name, clientId: client.clientId, clientSecret: client.clientSecret };
  }
  alert.success(i18n.t("mcp.client-created"));
  await load();
}

async function handleUpdated() {
  alert.success(i18n.t("mcp.client-updated"));
  await load();
}

async function handleRotate() {
  const client = confirmTarget.value;
  if (!client) {
    return;
  }

  const data = await rotateSecret(client.id);
  if (data) {
    newSecret.value = { clientName: client.name, clientId: data.clientId, clientSecret: data.clientSecret };
    alert.success(i18n.t("mcp.secret-rotated"));
  }
  else {
    alert.error(i18n.t("mcp.secret-rotate-failed"));
  }
}

async function handleDelete() {
  const client = confirmTarget.value;
  if (!client) {
    return;
  }

  if (await remove(client.id)) {
    // A secret shown for a deleted client is of no use any more
    if (newSecret.value?.clientId === client.clientId) {
      newSecret.value = null;
    }
    alert.success(i18n.t("mcp.client-deleted"));
  }
  else {
    alert.error(i18n.t("mcp.client-delete-failed"));
  }
}
</script>

<style scoped>
.client-id,
.secret-field :deep(input),
.server-url :deep(input) {
  font-family: monospace;
}

.client-id,
.redirect-uris code {
  word-break: break-all;
}
</style>
