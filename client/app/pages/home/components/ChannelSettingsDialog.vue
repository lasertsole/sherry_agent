<i18n lang="json">
{
  "en": {
    "extend": {
      "channelSettings": {
        "title": "{name} Settings",
        "enabled": "Enable Channel",
        "heartbeat": "Heartbeat",
        "heartbeatHint": "Automatically check pending tasks when idle",
        "cron": "Scheduled Tasks",
        "cronHint": "Allow tasks to run on a cron schedule",
        "config": "Channel Config",
        "configEmpty": "No config items",
        "configBool": "Boolean value",
        "configPlain": "Objects/arrays edited as JSON text",
        "configLoadFailed": "Failed to load channel config",
        "save": "Save",
        "cancel": "Cancel",
        "saveFailed": "Failed to save settings"
      },
      "tabs": {
        "channel": "Channels",
        "mcp": "MCP"
      },
      "title": "Extend",
      "empty": "Nothing here yet",
      "channelHint": "Channel data source API is under planning",
      "enabled": "Enabled",
      "disabled": "Disabled",
      "mcpHint": "No MCP servers configured"
    }
  },
  "ja": {
    "extend": {
      "channelSettings": {
        "title": "{name} 設定",
        "enabled": "チャンネルを有効化",
        "heartbeat": "ハートビート",
        "heartbeatHint": "アイドル時に保留タスクを自動チェック",
        "cron": "定期タスク",
        "cronHint": "cron 式による定期タスク実行を許可",
        "config": "チャンネル設定",
        "configEmpty": "設定項目なし",
        "configBool": "ブール値",
        "configPlain": "オブジェクト/配列は JSON テキストで編集",
        "configLoadFailed": "チャンネル設定の読み込みに失敗",
        "save": "保存",
        "cancel": "キャンセル",
        "saveFailed": "設定の保存に失敗しました"
      },
      "tabs": {
        "channel": "チャンネル",
        "mcp": "MCP"
      },
      "title": "拡張",
      "empty": "まだコンテンツがありません",
      "channelHint": "チャンネルのデータソース API は計画中です",
      "enabled": "有効",
      "disabled": "無効",
      "mcpHint": "MCP サーバーはまだ設定されていません"
    }
  },
  "ko": {
    "extend": {
      "channelSettings": {
        "title": "{name} 설정",
        "enabled": "채널 활성화",
        "heartbeat": "하트비트",
        "heartbeatHint": "유휴 시 대기 중인 작업 자동 확인",
        "cron": "정기 작업",
        "cronHint": "cron 표현식으로 주기적 작업 실행 허용",
        "config": "채널 구성",
        "configEmpty": "구성 항목 없음",
        "configBool": "불리언 값",
        "configPlain": "객체/배열은 JSON 텍스트로 편집",
        "configLoadFailed": "채널 구성 로드 실패",
        "save": "저장",
        "cancel": "취소",
        "saveFailed": "설정 저장에 실패했습니다"
      },
      "tabs": {
        "channel": "채널",
        "mcp": "MCP"
      },
      "title": "확장",
      "empty": "아직 내용이 없습니다",
      "channelHint": "채널 데이터 소스 API는 계획 중입니다",
      "enabled": "활성화",
      "disabled": "비활성화",
      "mcpHint": "MCP 서버가 아직 설정되지 않았습니다"
    }
  },
  "zh": {
    "extend": {
      "channelSettings": {
        "title": "{name} 设置",
        "enabled": "启用频道",
        "heartbeat": "心跳",
        "heartbeatHint": "空闲时自动检查待办任务",
        "cron": "定时任务",
        "cronHint": "允许按 cron 表达式定时执行任务",
        "config": "频道配置",
        "configEmpty": "无配置项",
        "configBool": "布尔值",
        "configPlain": "对象/数组以 JSON 文本编辑",
        "configLoadFailed": "加载频道配置失败",
        "save": "保存",
        "cancel": "取消",
        "saveFailed": "保存设置失败"
      },
      "tabs": {
        "channel": "频道",
        "mcp": "MCP"
      },
      "title": "扩展",
      "empty": "暂无内容",
      "channelHint": "频道数据源接口待规划",
      "enabled": "启用",
      "disabled": "禁用",
      "mcpHint": "MCP 服务器尚无配置"
    }
  }
}
</i18n>

<template>
  <Dialog
    v-model:visible="visible"
    :header="t('extend.channelSettings.title', { name: channel?.display_name ?? channel?.name ?? '' })"
    :modal="true"
    :closable="false"
    class="w-[95vw] sm:w-[480px]"
    @show="init">
    <div class="flex flex-col gap-1">
      <!-- enabled -->
      <div class="flex items-center justify-between gap-3 py-2">
        <div class="flex flex-col">
          <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{
            t('extend.channelSettings.enabled')
          }}</span>
        </div>
        <ToggleSwitch v-model="form.enabled" />
      </div>
      <Divider class="my-1" />

      <!-- heartbeat -->
      <div class="flex items-center justify-between gap-3 py-2">
        <div class="flex flex-col">
          <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{
            t('extend.channelSettings.heartbeat')
          }}</span>
          <span class="text-xs text-gray-400 dark:text-gray-500">{{ t('extend.channelSettings.heartbeatHint') }}</span>
        </div>
        <ToggleSwitch v-model="form.heartbeat" />
      </div>
      <Divider class="my-1" />

      <!-- cron -->
      <div class="flex items-center justify-between gap-3 py-2">
        <div class="flex flex-col">
          <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{
            t('extend.channelSettings.cron')
          }}</span>
          <span class="text-xs text-gray-400 dark:text-gray-500">{{ t('extend.channelSettings.cronHint') }}</span>
        </div>
        <ToggleSwitch v-model="form.cron" />
      </div>
      <Divider class="my-1" />

      <!-- per-channel config.json -->
      <div class="flex flex-col gap-2 py-2">
        <div class="flex items-center justify-between">
          <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{
            t('extend.channelSettings.config')
          }}</span>
          <span
            v-if="configEmpty"
            class="text-xs text-gray-400 dark:text-gray-500"
            >{{ t('extend.channelSettings.configEmpty') }}</span
          >
        </div>

        <div
          v-for="(field, key) in configFields"
          :key="key"
          class="flex items-start justify-between gap-3 py-1">
          <div class="flex min-w-0 flex-col">
            <span class="text-sm text-gray-700 dark:text-gray-300">{{ key }}</span>
            <span
              v-if="isBool(field.value)"
              class="text-xs text-gray-400 dark:text-gray-500"
              >{{ t('extend.channelSettings.configBool') }}</span
            >
            <span
              v-else
              class="text-xs text-gray-400 dark:text-gray-500"
              >{{ t('extend.channelSettings.configPlain') }}</span
            >
          </div>

          <div class="min-w-0 basis-3/5">
            <!-- boolean -> toggle -->
            <ToggleSwitch
              v-if="isBool(field.value)"
              v-model="field.value" />
            <!-- number -> numeric input -->
            <InputNumber
              v-else-if="isNumber(field.value)"
              v-model="field.value"
              class="w-full"
              mode="decimal" />
            <!-- everything else -> text input (JSON string for objects/arrays) -->
            <InputText
              v-else
              v-model="field.value"
              class="w-full"
              size="small" />
          </div>
        </div>
      </div>

      <p
        v-if="error"
        class="m-0 mt-2 text-xs text-red-600 dark:text-red-400">
        {{ error }}
      </p>
    </div>

    <template #footer>
      <div class="flex gap-2 justify-end">
        <Button
          :label="t('extend.channelSettings.cancel')"
          icon="pi pi-times"
          severity="secondary"
          @click="visible = false" />
        <Button
          :label="t('extend.channelSettings.save')"
          icon="pi pi-check"
          :loading="saving"
          @click="handleSave" />
      </div>
    </template>
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed } from 'vue';
import { useI18n } from 'vue-i18n';
import { updateChannel, getChannelConfig, updateChannelConfig } from '@/composables/bridge';
import type { ChannelInfo, ChannelConfig } from '@/composables/bridge';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{
  modelValue: boolean;
  /** The channel being configured (the dialog has no effect when null). */
  channel: ChannelInfo | null;
}>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

/** Edit form; synced back to the parent after a successful save (the parent re-fetches the channel list via @saved). */
const form = ref<{ enabled: boolean; heartbeat: boolean; cron: boolean }>({
  enabled: false,
  heartbeat: false,
  cron: false
});

/**
 * Edit state for each config field. value keeps the original JSON type;
 * objects/arrays are shown as strings in the UI and parsed back into objects on save.
 */
/** JSON config value as kept in the edit form: booleans → ToggleSwitch, numbers → InputNumber, everything else → InputText. */
type ConfigValue = boolean | number | string | null;

interface ConfigField {
  value: ConfigValue;
}
const configFields = ref<Record<string, ConfigField>>({});
const saving = ref(false);
const error = ref('');

const configEmpty = computed(() => Object.keys(configFields.value).length === 0);

function isBool(v: unknown): v is boolean {
  return typeof v === 'boolean';
}
function isNumber(v: unknown): v is number {
  return typeof v === 'number';
}

/** Convert the config object into the frontend edit structure; objects/arrays are serialized to JSON strings so they can be edited in InputText. */
function normalizeConfig(raw: ChannelConfig): Record<string, ConfigField> {
  const out: Record<string, ConfigField> = {};
  for (const [k, v] of Object.entries(raw)) {
    out[k] = { value: normalizeValue(v) };
  }
  return out;
}

function normalizeValue(v: unknown): ConfigValue {
  if (v !== null && typeof v === 'object') {
    return JSON.stringify(v, null, 0);
  }
  if (v === null || typeof v === 'boolean' || typeof v === 'number' || typeof v === 'string') {
    return v;
  }
  return String(v);
}

/** On save, parse stringified objects/arrays back to JSON; keep the string if parsing fails. */
function denormalizeValue(v: unknown): unknown {
  if (typeof v !== 'string') return v;
  const trimmed = v.trim();
  // Only attempt to parse strings that look like JSON text.
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      return JSON.parse(trimmed);
    } catch {
      // Keep the original string; the frontend echoes it back.
      return v;
    }
  }
  return v;
}

/** Triggered by @show: fetch the channel's local config and prefill the form and config fields. */
const init = async () => {
  error.value = '';
  const ch = props.channel;
  form.value = {
    enabled: ch?.enabled ?? false,
    heartbeat: ch?.heartbeat ?? false,
    cron: ch?.cron ?? false
  };
  configFields.value = {};
  if (ch) {
    try {
      const res = await getChannelConfig(ch.name);
      configFields.value = normalizeConfig(res.config ?? {});
    } catch (e) {
      error.value = `${t('extend.channelSettings.configLoadFailed')}: ${e}`;
      console.error('[ChannelSettingsDialog] Failed to load channel config:', e);
    }
  }
};

const handleSave = async () => {
  const ch = props.channel;
  if (!ch) return;
  saving.value = true;
  error.value = '';
  try {
    await updateChannel(ch.name, {
      enabled: form.value.enabled,
      heartbeat: form.value.heartbeat,
      cron: form.value.cron
    });

    // Write the edited config back (preserving each value's original JSON type).
    const cfg: ChannelConfig = {};
    for (const [k, f] of Object.entries(configFields.value)) {
      cfg[k] = denormalizeValue(f.value);
    }
    await updateChannelConfig(ch.name, cfg);

    emits('saved');
    visible.value = false;
  } catch (e) {
    error.value = `${t('extend.channelSettings.saveFailed')}: ${e}`;
    console.error('[ChannelSettingsDialog] Failed to save channel settings:', e);
  } finally {
    saving.value = false;
  }
};
</script>
