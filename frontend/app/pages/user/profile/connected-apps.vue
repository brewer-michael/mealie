<template>
  <v-container class="narrow-container">
    <BasePageTitle divider>
      <template #header>
        <v-img
          width="100%"
          max-height="200px"
          max-width="200px"
          src="/svgs/manage-api-tokens.svg"
        />
      </template>
      <template #title>
        {{ $t("mcp.connected-apps") }}
      </template>
      {{ $t("mcp.connected-apps-description") }}
    </BasePageTitle>
    <UserMcpConnections />
  </v-container>
</template>

<script setup lang="ts">
import UserMcpConnections from "~/components/Domain/User/UserMcpConnections.vue";

const i18n = useI18n();

useSeoMeta({
  title: i18n.t("mcp.connected-apps"),
});
</script>
