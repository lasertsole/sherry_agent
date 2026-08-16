<template>
  <Dialog
    v-model:visible="visible"
    :header="t('extend.channelSettings.title', { name: channel?.display_name ?? channel?.name ?? '' })"
    :modal="true"
    :closable="true"
    class="w-[95vw] sm:w-[480px]"
    @show="init">
    <div class="flex flex-col gap-1">
      <!-- enabled -->
      <div class="flex items-center justify-between gap-3 py-2">
        <div class="flex flex-col">
          <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ t('extend.channelSettings.enabled') }}</span>
        </div>
        <ToggleSwitch v-model="form.enabled" />
      </div>
      <Divider class="my-1" />

      <!-- heartbeat -->
      <div class="flex items-center justify-between gap-3 py-2">
        <div class="flex flex-col">
          <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ t('extend.channelSettings.heartbeat') }}</span>
          <span class="text-xs text-gray-400 dark:text-gray-500">{{ t('extend.channelSettings.heartbeatHint') }}</span>
        </div>
        <ToggleSwitch v-model="form.heartbeat" />
      </div>
      <Divider class="my-1" />

      <!-- cron -->
      <div class="flex items-center justify-between gap-3 py-2">
        <div class="flex flex-col">
          <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ t('extend.channelSettings.cron') }}</span>
          <span class="text-xs text-gray-400 dark:text-gray-500">{{ t('extend.channelSettings.cronHint') }}</span>
        </div>
        <ToggleSwitch v-model="form.cron" />
      </div>
      <Divider class="my-1" />

      <!-- per-channel config.json -->
      <div class="flex flex-col gap-2 py-2">
        <div class="flex items-center justify-between">
          <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ t('extend.channelSettings.config') }}</span>
          <span v-if="configEmpty" class="text-xs text-gray-400 dark:text-gray-500">{{ t('extend.channelSettings.configEmpty') }}</span>
        </div>

        <div
          v-for="(field, key) in configFields"
          :key="key"
          class="flex items-start justify-between gap-3 py-1">
          <div class="flex min-w-0 flex-col">
            <span class="text-sm text-gray-700 dark:text-gray-300">{{ key }}</span>
            <span v-if="isBool(field.value)" class="text-xs text-gray-400 dark:text-gray-500">{{ t('extend.channelSettings.configBool') }}</span>
            <span v-else class="text-xs text-gray-400 dark:text-gray-500">{{ t('extend.channelSettings.configPlain') }}</span>
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
        class="m-0 mt-2 text-xs text-red-600 dark:text-red-400">{{ error }}</p>
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

const { t } = useI18n();

const props = defineProps<{
  modelValue: boolean;
  /** 当前要设置的频道（为 null 时弹窗不生效）。 */
  channel: ChannelInfo | null;
}>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emits('update:modelValue', v),
});

/** 编辑表单，保存成功后同步到父级（父级通过 @saved 重新拉取频道列表）。 */
const form = ref<{ enabled: boolean; heartbeat: boolean; cron: boolean }>({
  enabled: false,
  heartbeat: false,
  cron: false,
});

/**
 * 每个 config 字段的编辑状态。value 保持原始 JSON 类型；
 * 对象/数组在 UI 上以字符串展示，保存时再解析回对象。
 */
interface ConfigField {
  value: unknown;
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

/** 把 config 对象转成前端编辑结构；对象/数组序列化为 JSON 字符串以便在 InputText 中编辑。 */
function normalizeConfig(raw: ChannelConfig): Record<string, ConfigField> {
  const out: Record<string, ConfigField> = {};
  for (const [k, v] of Object.entries(raw)) {
    out[k] = { value: normalizeValue(v) };
  }
  return out;
}

function normalizeValue(v: unknown): unknown {
  if (v !== null && typeof v === 'object') {
    return JSON.stringify(v, null, 0);
  }
  return v;
}

/** 保存时把字符串形式的对象/数组解析回 JSON；无法解析则保持字符串。 */
function denormalizeValue(v: unknown): unknown {
  if (typeof v !== 'string') return v;
  const trimmed = v.trim();
  // 仅对看起来像 JSON 文本的字符串尝试解析。
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      return JSON.parse(trimmed);
    } catch {
      // 保留原始字符串，前端会回显。
      return v;
    }
  }
  return v;
}

/** @show 触发：拉取频道本地 config 并预填表单与配置字段。 */
const init = async () => {
  error.value = '';
  const ch = props.channel;
  form.value = {
    enabled: ch?.enabled ?? false,
    heartbeat: ch?.heartbeat ?? false,
    cron: ch?.cron ?? false,
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
      cron: form.value.cron,
    });

    // 将编辑后的 config 回写（保留各值的原始 JSON 类型）。
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
