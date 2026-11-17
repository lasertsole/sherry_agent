<template>
  <div
    v-if="auth.tenants.length > 1"
    class="flex items-center gap-1"
    data-test="tenant-switcher">
    <i
      class="pi pi-server text-xs text-gray-500 dark:text-gray-400"
      :title="t('tenant.switch')"
      aria-hidden="true" />
    <Select
      :model-value="auth.activeTenant"
      :options="auth.tenants"
      option-label="tenant_id"
      option-value="tenant_id"
      :disabled="switching"
      size="small"
      class="min-w-28"
      :aria-label="t('tenant.switch')"
      :title="t('tenant.switch')"
      data-test="tenant-select"
      @update:model-value="onSelect" />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { useI18n } from 'vue-i18n';

const auth = useAuthStore();
const { t } = useI18n();
const switching = ref(false);

onMounted(() => {
  void auth.loadTenants();
});

/**
 * Switch cells. The store reloads the page on success because every cached
 * session, memory entry and WebSocket belongs to the previous cell.
 * @param value Selected tenant id from the Select.
 */
async function onSelect(value: unknown): Promise<void> {
  const tenantId = String(value ?? '');
  if (!tenantId || tenantId === auth.activeTenant) return;
  switching.value = true;
  try {
    await auth.switchTenant(tenantId);
  } finally {
    switching.value = false;
  }
}
</script>

<i18n lang="json">
{
  "zh": {
    "tenant": {
      "switch": "切换 Cell"
    }
  },
  "en": {
    "tenant": {
      "switch": "Switch cell"
    }
  },
  "ja": {
    "tenant": {
      "switch": "セルを切り替え"
    }
  },
  "ko": {
    "tenant": {
      "switch": "셀 전환"
    }
  }
}
</i18n>
