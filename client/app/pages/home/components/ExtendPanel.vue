<i18n lang="json">
{
  "en": {
    "extend": {
      "empty": "Nothing here yet",
      "channelHint": "Channel data source API is under planning",
      "enabled": "Enabled",
      "disabled": "Disabled",
      "mcpHint": "No MCP servers configured",
      "tabs": {
        "channel": "Channels",
        "mcp": "MCP"
      },
      "mcp": {
        "openFolder": "Open folder",
        "refresh": "Refresh",
        "restart": "Restart backend",
        "restarted": "Backend restarted; new servers are live.",
        "sourceUser": "user",
        "sourceBundled": "bundled",
        "parseError": "Parse error",
        "dropHint": "Drop MCP configs into {dir} — they load on the next backend start.",
        "shapeHint": "Two shapes: mcp_servers/<name>.json (flat), or mcp_servers/<name>/config.json ($here/ points at the folder)."
      }
    }
  },
  "ja": {
    "extend": {
      "empty": "まだコンテンツがありません",
      "channelHint": "チャンネルのデータソース API は計画中です",
      "enabled": "有効",
      "disabled": "無効",
      "mcpHint": "MCP サーバーはまだ設定されていません",
      "tabs": {
        "channel": "チャンネル",
        "mcp": "MCP"
      },
      "mcp": {
        "openFolder": "フォルダを開く",
        "refresh": "更新",
        "restart": "バックエンドを再起動",
        "restarted": "バックエンドを再起動しました。新しいサーバーが有効です。",
        "sourceUser": "ユーザー",
        "sourceBundled": "内蔵",
        "parseError": "解析エラー",
        "dropHint": "{dir} に MCP 設定を置くと、次回のバックエンド起動時に読み込まれます。",
        "shapeHint": "2 つの形：mcp_servers/<名前>.json（フラット）、または mcp_servers/<名前>/config.json（$here/ はそのフォルダを指します）。"
      }
    }
  },
  "ko": {
    "extend": {
      "empty": "아직 내용이 없습니다",
      "channelHint": "채널 데이터 소스 API는 계획 중입니다",
      "enabled": "활성화",
      "disabled": "비활성화",
      "mcpHint": "MCP 서버가 아직 설정되지 않았습니다",
      "tabs": {
        "channel": "채널",
        "mcp": "MCP"
      },
      "mcp": {
        "openFolder": "폴더 열기",
        "refresh": "새로 고침",
        "restart": "백엔드 재시작",
        "restarted": "백엔드가 재시작되어 새 서버가 활성화되었습니다.",
        "sourceUser": "사용자",
        "sourceBundled": "내장",
        "parseError": "구문 오류",
        "dropHint": "{dir} 에 MCP 설정을 넣으면 다음 백엔드 시작 시 로드됩니다.",
        "shapeHint": "두 형태: mcp_servers/<이름>.json(플랫) 또는 mcp_servers/<이름>/config.json($here/는 해당 폴더를 가리킵니다)."
      }
    }
  },
  "zh": {
    "extend": {
      "empty": "暂无内容",
      "channelHint": "频道数据源接口待规划",
      "enabled": "启用",
      "disabled": "禁用",
      "mcpHint": "MCP 服务器尚无配置",
      "tabs": {
        "channel": "频道",
        "mcp": "MCP"
      },
      "mcp": {
        "openFolder": "打开文件夹",
        "refresh": "刷新",
        "restart": "重启后端",
        "restarted": "后端已重启，新配置已生效。",
        "sourceUser": "用户",
        "sourceBundled": "内置",
        "parseError": "解析失败",
        "dropHint": "把 MCP 配置放进 {dir}，重启后端后自动加载。",
        "shapeHint": "两种形态：mcp_servers/<名字>.json（扁平），或 mcp_servers/<名字>/config.json（$here/ 指向该文件夹）。"
      }
    }
  }
}
</i18n>

<template>
  <!-- Right-sidebar tab body: mounted with the tab, which drives the load
       (the sidebar owns the tab label and the close button). -->
  <div class="flex flex-col h-full min-h-0 p-4">
    <TabView
      v-model:activeIndex="activeTab"
      class="flex-1 min-h-0">
      <!-- ===== Channels tab ===== -->
      <TabPanel
        value="channel"
        :header="t('extend.tabs.channel')">
        <div class="flex flex-col gap-3 h-full">
          <!-- Loading -->
          <div
            v-if="loading"
            class="flex flex-1 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 py-16 text-gray-400">
            <i class="pi pi-spin pi-spinner text-3xl" />
            <span class="text-sm">{{ t('extend.empty') }}</span>
          </div>

          <!-- Empty state: no channels -->
          <div
            v-else-if="channels.length === 0"
            class="flex flex-1 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 py-16 text-gray-400">
            <i class="pi pi-link text-3xl" />
            <span class="text-sm">{{ t('extend.empty') }}</span>
            <span class="text-xs">{{ t('extend.channelHint') }}</span>
          </div>

          <!-- Channel grid -->
          <div
            v-else
            class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3 flex-1 content-start">
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
        <div class="flex flex-col gap-3 h-full">
          <!-- Actions: the drop folder (where configs go) + a manual refresh. -->
          <div class="flex flex-wrap items-center gap-2">
            <Button
              size="small"
              icon="pi pi-folder-open"
              :label="t('extend.mcp.openFolder')"
              :disabled="!userDir"
              @click="openMcpFolder" />
            <Button
              size="small"
              severity="secondary"
              text
              icon="pi pi-refresh"
              :label="t('extend.mcp.refresh')"
              :loading="mcpLoading"
              @click="refreshMcpServers" />
          </div>

          <!-- Discovered servers: bundled defaults + the user drop folder. -->
          <div
            v-if="servers.length"
            class="flex flex-col gap-1">
            <div
              v-for="server in servers"
              :key="`${server.source}:${server.name}`"
              class="flex items-center gap-2 rounded-lg border border-gray-100 dark:border-gray-800 px-3 py-2"
              :data-test="`mcp-server-${server.source}-${server.name}`">
              <i
                class="pi text-xs"
                :class="
                  server.has_error ? 'pi-exclamation-triangle text-amber-500' : 'pi-circle-fill text-emerald-500'
                " />
              <span class="min-w-0 truncate text-sm font-medium">{{ server.name }}</span>
              <span
                class="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-500 dark:bg-gray-800 dark:text-gray-400">
                {{ server.source === 'user' ? t('extend.mcp.sourceUser') : t('extend.mcp.sourceBundled') }}
              </span>
              <span
                v-if="server.command"
                class="min-w-0 truncate font-mono text-xs text-gray-400"
                >{{ server.command }}</span
              >
              <span
                v-if="server.has_error"
                class="min-w-0 truncate text-xs text-amber-600 dark:text-amber-400">
                {{ server.error || t('extend.mcp.parseError') }}
              </span>
            </div>
          </div>
          <div
            v-else-if="mcpLoaded && !mcpLoading"
            class="flex flex-1 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 py-16 text-gray-400">
            <i class="pi pi-plug text-3xl" />
            <span class="text-sm">{{ t('extend.empty') }}</span>
            <span class="text-xs">{{ t('extend.mcpHint') }}</span>
          </div>

          <!-- The convention + the restart hand-off (a fresh process re-scans). -->
          <div class="flex flex-col gap-1 rounded-lg bg-gray-50 dark:bg-gray-900 p-3">
            <p class="m-0 text-xs text-gray-500 dark:text-gray-400">
              {{ t('extend.mcp.dropHint', { dir: userDir }) }}
            </p>
            <p class="m-0 text-xs text-gray-400">
              {{ t('extend.mcp.shapeHint') }}
            </p>
            <div
              v-if="canRestart"
              class="mt-1 flex items-center gap-2">
              <Button
                size="small"
                severity="secondary"
                icon="pi pi-refresh"
                :label="t('extend.mcp.restart')"
                :loading="restarting"
                @click="onRestartBackend" />
              <span
                v-if="restartHint"
                class="text-xs text-emerald-600 dark:text-emerald-400">
                {{ t('extend.mcp.restarted') }}
              </span>
            </div>
          </div>
        </div>
      </TabPanel>
    </TabView>

    <!-- Channel settings dialog (opened by clicking a card) -->
    <ChannelSettingsDialog
      v-model="showSettings"
      :channel="selectedChannel"
      @saved="loadChannels" />
  </div>
</template>

<style scoped>
/* Fill the tab: the strip keeps its own height and the panels area is what
   stretches (and scrolls), so a short body — an empty state, one channel card —
   spans the whole tab instead of floating at the top. Same chain as
   ConfigPanel / PersonaPanel; the panel GROWS but never SHRINKS below its
   content (`flex: 1 0 auto`), so a long channel grid scrolls instead of being
   squashed. */
:deep(.p-tabview) {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}

/* This PrimeVue version renders the strip as .p-tabview-tablist-container
   (there is no .p-tabview-nav): keep it at its own height so the panels area
   below is the only thing that scrolls. */
:deep(.p-tabview-tablist-container) {
  flex-shrink: 0;
}

:deep(.p-tabview-panels) {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

:deep(.p-tabview-panel) {
  display: flex;
  flex-direction: column;
  flex: 1 0 auto;
  min-height: 0;
}
</style>

<script lang="ts" setup>
import { ref, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import type { ChannelInfo } from '@/composables/bridge';
import ChannelSettingsDialog from './ChannelSettingsDialog.vue';
import { logUtil } from '~/utils/log';

const { t } = useI18n({ useScope: 'local' });

// MCP discovery: the loader's merged view + the drop-folder actions.
// Explicit import (not the auto-import): the panel's tests mount this file
// without the unimport injection, so a bare symbol would be undefined there.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { useMcpServers } from '~/composables/use-mcp-servers';

const {
  servers,
  userDir,
  loading: mcpLoading,
  loaded: mcpLoaded,
  restarting,
  restartHint,
  canRestart,
  refreshMcpServers,
  openMcpFolder,
  restartBackendForMcp
} = useMcpServers();

/** Restart the backend and confirm it in place (the MCP list re-reads after). */
const onRestartBackend = async () => {
  await restartBackendForMcp();
  restartHint.value = true;
};

/** Currently active tab (0=channels, 1=MCP) */
const activeTab = ref(0);

const loading = ref(false);
const channels = ref<ChannelInfo[]>([]);

/** Channel settings dialog state: the selected channel + dialog visibility. */
const selectedChannel = ref<ChannelInfo | null>(null);
const showSettings = ref(false);

/**
 * Card click → open that channel's settings dialog.
 * @param ch
 */
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
    logUtil.e('[ExtendPanel] Failed to load channels:', e);
  } finally {
    loading.value = false;
  }
};

onMounted(loadChannels);
</script>
