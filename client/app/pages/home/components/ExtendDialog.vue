<i18n lang="json">
{
  "en": {
    "extend": {
      "title": "Extend",
      "empty": "Nothing here yet",
      "channelHint": "Channel data source API is under planning",
      "enabled": "Enabled",
      "disabled": "Disabled",
      "mcpHint": "No MCP servers configured",
      "tabs": {
        "channel": "Channels",
        "mcp": "MCP"
      }
    }
  },
  "ja": {
    "extend": {
      "title": "拡張",
      "empty": "まだコンテンツがありません",
      "channelHint": "チャンネルのデータソース API は計画中です",
      "enabled": "有効",
      "disabled": "無効",
      "mcpHint": "MCP サーバーはまだ設定されていません",
      "tabs": {
        "channel": "チャンネル",
        "mcp": "MCP"
      }
    }
  },
  "ko": {
    "extend": {
      "title": "확장",
      "empty": "아직 내용이 없습니다",
      "channelHint": "채널 데이터 소스 API는 계획 중입니다",
      "enabled": "활성화",
      "disabled": "비활성화",
      "mcpHint": "MCP 서버가 아직 설정되지 않았습니다",
      "tabs": {
        "channel": "채널",
        "mcp": "MCP"
      }
    }
  },
  "zh": {
    "extend": {
      "title": "扩展",
      "empty": "暂无内容",
      "channelHint": "频道数据源接口待规划",
      "enabled": "启用",
      "disabled": "禁用",
      "mcpHint": "MCP 服务器尚无配置",
      "tabs": {
        "channel": "频道",
        "mcp": "MCP"
      }
    }
  }
}
</i18n>

<template>
  <Dialog
    v-model:visible="visible"
    :header="t('extend.title')"
    :modal="true"
    :closable="true"
    class="w-[95vw] md:w-[800px]">
    <TabView v-model:activeIndex="activeTab">
      <!-- ===== Channels tab ===== -->
      <TabPanel
        value="channel"
        :header="t('extend.tabs.channel')">
        <div class="flex flex-col gap-3">
          <!-- Loading -->
          <div
            v-if="loading"
            class="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 py-16 text-gray-400">
            <i class="pi pi-spin pi-spinner text-3xl" />
            <span class="text-sm">{{ t('extend.empty') }}</span>
          </div>

          <!-- Empty state: no channels -->
          <div
            v-else-if="channels.length === 0"
            class="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 py-16 text-gray-400">
            <i class="pi pi-link text-3xl" />
            <span class="text-sm">{{ t('extend.empty') }}</span>
            <span class="text-xs">{{ t('extend.channelHint') }}</span>
          </div>

          <!-- Channel grid -->
          <div
            v-else
            class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
            <div
              v-for="ch in channels"
              :key="ch.name"
              class="flex flex-col items-center gap-2 rounded-lg border border-gray-200 dark:border-gray-700 p-4 hover:border-primary transition-colors cursor-pointer"
              role="button"
              tabindex="0"
              @click="openSettings(ch)"
              @keydown.enter.prevent="openSettings(ch)"
              @keydown.space.prevent="openSettings(ch)">
              <div class="relative">
                <img
                  v-if="ch.icon"
                  :src="ch.icon"
                  :alt="ch.display_name"
                  class="w-12 h-12 rounded-lg object-cover" />
                <div
                  v-else
                  class="w-12 h-12 rounded-lg bg-gray-100 dark:bg-gray-800 flex items-center justify-center">
                  <i class="pi pi-link text-2xl text-gray-400" />
                </div>
                <span
                  class="absolute -top-1 -right-1 w-3 h-3 rounded-full border-2 border-white dark:border-gray-900"
                  :class="ch.enabled ? 'bg-green-500' : 'bg-gray-400'" />
              </div>
              <span class="text-sm font-medium text-gray-800 dark:text-gray-200 text-center break-all">
                {{ ch.display_name }}
              </span>
              <span
                class="text-xs px-2 py-0.5 rounded-full"
                :class="
                  ch.enabled
                    ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                    : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
                ">
                {{ ch.enabled ? t('extend.enabled') : t('extend.disabled') }}
              </span>
            </div>
          </div>
        </div>
      </TabPanel>

      <!-- ===== MCP tab ===== -->
      <TabPanel
        value="mcp"
        :header="t('extend.tabs.mcp')">
        <div class="flex flex-col gap-3">
          <!-- Empty state placeholder: no MCP servers configured yet, show a placeholder for now -->
          <div
            class="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 py-16 text-gray-400">
            <i class="pi pi-plug text-3xl" />
            <span class="text-sm">{{ t('extend.empty') }}</span>
            <span class="text-xs">{{ t('extend.mcpHint') }}</span>
          </div>
        </div>
      </TabPanel>
    </TabView>

    <!-- Channel settings dialog (opened by clicking a card) -->
    <ChannelSettingsDialog
      v-model="showSettings"
      :channel="selectedChannel"
      @saved="loadChannels" />
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { listChannels } from '@/composables/bridge';
import type { ChannelInfo } from '@/composables/bridge';
import ChannelSettingsDialog from './ChannelSettingsDialog.vue';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

/** Currently active tab (0=channels, 1=MCP) */
const activeTab = ref(0);

const loading = ref(false);
const channels = ref<ChannelInfo[]>([]);

/** Channel settings dialog state: the selected channel + dialog visibility. */
const selectedChannel = ref<ChannelInfo | null>(null);
const showSettings = ref(false);

/** Card click → open that channel's settings dialog. */
const openSettings = (ch: ChannelInfo) => {
  selectedChannel.value = ch;
  showSettings.value = true;
};

const loadChannels = async () => {
  loading.value = true;
  try {
    const resp = await listChannels();
    channels.value = resp.channels ?? [];
  } catch (e) {
    console.error('[ExtendDialog] Failed to load channels:', e);
  } finally {
    loading.value = false;
  }
};

onMounted(loadChannels);
</script>
