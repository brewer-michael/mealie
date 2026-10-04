<template>
  <div>
    <!-- A failed refresh keeps showing the connections loaded before it -->
    <v-alert
      v-if="loadFailed"
      type="error"
      density="compact"
      variant="tonal"
      class="mb-4"
    >
      <div class="d-flex align-center flex-wrap">
        <span class="me-2">{{ $t("mcp.connections-load-failed") }}</span>
        <v-btn class="ms-auto" variant="text" size="small" :loading="loading" @click="load">
          {{ $t("mcp.try-again") }}
        </v-btn>
      </div>
    </v-alert>
    <AppLoader v-if="!loaded && loading" />
    <p v-else-if="loaded && !connections.length" class="text-body-large text-center my-6 no-connections">
      {{ $t("mcp.no-connected-apps") }}
    </p>

    <v-list v-if="connections.length">
      <template v-for="connection in connections" :key="connection.clientId">
        <v-list-item class="connection" lines="three">
          <v-list-item-title>
            {{ connection.clientName }}
          </v-list-item-title>
          <v-list-item-subtitle class="permissions">
            {{ canWrite(connection.scopes) ? $t("mcp.permission-read-write") : $t("mcp.permission-read") }}
          </v-list-item-subtitle>
          <v-list-item-subtitle class="usage">
            {{ $t("mcp.connected-on", { date: formatDateTime(connection.createdAt, locale) }) }}
            ·
            {{ connection.lastUsedAt
              ? $t("mcp.last-used", { date: formatDateTime(connection.lastUsedAt, locale) })
              : $t("mcp.never-used") }}
          </v-list-item-subtitle>
          <template #append>
            <v-btn
              variant="text"
              size="small"
              color="error"
              :prepend-icon="$globals.icons.delete"
              @click="confirmDisconnect(connection)"
            >
              {{ $t("mcp.disconnect") }}
            </v-btn>
          </template>
        </v-list-item>
        <v-divider class="mx-2 my-2" />
      </template>
    </v-list>

    <BaseDialog
      v-model="confirmOpen"
      bottom-sheet
      :title="$t('mcp.disconnect-app')"
      color="error"
      :icon="$globals.icons.alertCircle"
      can-confirm
      @confirm="handleDisconnect"
    >
      <v-card-text>
        {{ $t("mcp.disconnect-confirm", { name: confirmTarget?.clientName ?? "" }) }}
      </v-card-text>
    </BaseDialog>
  </div>
</template>

<script setup lang="ts">
import { canWrite, formatDateTime, useMcpConnections } from "~/composables/use-mcp";
import { alert } from "~/composables/use-toast";
import type { McpConnectionOut } from "~/lib/api/types/mcp";

/** Profile → Connected Apps: the apps the user connected with OAuth, and disconnecting them */
const i18n = useI18n();
const { connections, loading, loaded, loadFailed, load, disconnect } = useMcpConnections();

const locale = computed(() => i18n.locale.value);
const confirmOpen = ref(false);
const confirmTarget = ref<McpConnectionOut | null>(null);

onMounted(load);

function confirmDisconnect(connection: McpConnectionOut) {
  confirmTarget.value = connection;
  confirmOpen.value = true;
}

async function handleDisconnect() {
  const connection = confirmTarget.value;
  if (!connection) {
    return;
  }

  if (await disconnect(connection.clientId)) {
    alert.success(i18n.t("mcp.disconnected"));
  }
  else {
    alert.error(i18n.t("mcp.disconnect-failed"));
  }
}
</script>
