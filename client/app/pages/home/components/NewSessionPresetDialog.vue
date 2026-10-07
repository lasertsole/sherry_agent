<template>
  <Dialog
    v-model:visible="visible"
    :header="t('personaPreset.selectTitle')"
    :modal="true"
    :closable="true"
    :dismissableMask="false"
    :style="{ width: '420px' }"
    data-test="new-session-dialog">
    <div class="flex flex-col gap-3">
      <p class="m-0 text-xs text-gray-500 dark:text-gray-400">{{ t('personaPreset.selectHint') }}</p>
      <!-- The catalogue: built-ins first (编程助手 is the default), then the saved presets. -->
      <div
        class="flex max-h-[46vh] flex-col gap-1 overflow-y-auto"
        data-test="new-session-preset-list">
        <div
          v-for="entry in entries"
          :key="entry.id"
          role="button"
          tabindex="0"
          class="flex cursor-pointer items-center justify-between gap-2 rounded-lg border border-solid border-gray-light px-3 py-2 text-sm transition-colors dark:border-[#555]"
          :class="
            selectedId === entry.id
              ? 'bg-[#c1d6e5]!'
              : 'bg-white md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5] dark:bg-[#2a2a36]/[0.6]'
          "
          :data-test="`new-session-preset-option-${entry.id}`"
          @click="selectedId = entry.id">
          <span class="flex min-w-0 items-center gap-1.5 text-theme-main">
            <i
              v-if="selectedId === entry.id"
              class="pi pi-check text-xs"
              aria-hidden="true"></i>
            <span class="truncate">{{ entryName(entry, t) }}</span>
          </span>
          <span
            v-if="entry.badgeKey"
            class="shrink-0 rounded-full bg-gray-200 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-300">
            {{ t(entry.badgeKey) }}
          </span>
        </div>
      </div>
    </div>
    <template #footer>
      <div class="flex justify-end gap-2">
        <Button
          :label="t('config.cancel')"
          severity="secondary"
          :disabled="creating"
          @click="visible = false" />
        <Button
          :label="t('personaPreset.create')"
          icon="pi pi-check"
          :loading="creating"
          :disabled="!selectedId"
          data-test="new-session-confirm"
          @click="confirm" />
      </div>
    </template>
  </Dialog>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { ensureSessionCharacter } from './SessionSidebar.vue';
import { logUtil } from '~/utils/log';

const { t, locale } = useI18n();

/** Open state, driven by the shell (`pages/home/index.vue`). */
const visible = defineModel<boolean>('visible', { required: true });

const router = useRouter();
/** Locale-aware path builder (`/home/<sid>` under the active locale). */
const localePath = useLocalePath();

/** The saved presets (shared singleton); built-ins come from the catalogue. */
const { presets } = usePersonaPresets();

/** The agent-config store: its catalogue is what 纯净 / 情感陪伴 derive from. */
const agentStore = useAgentConfigStore();

/** Built-ins + saved presets, in the shared display order. */
const entries = computed(() => catalogEntries(presets.value));

/** Chosen catalogue id (the default preset until the user picks another). */
const selectedId = ref<string>(DEFAULT_PRESET_ID);

/** True while the preset is being applied / the session created. */
const creating = ref(false);

// Every open starts from the default preset (a fresh mandatory choice).
watch(visible, open => {
  if (open) {
    selectedId.value = DEFAULT_PRESET_ID;
    creating.value = false;
  }
});

/**
 * Confirm: apply the chosen preset (persona files + character), bind it to the
 * new session, then create the session and navigate to it.
 *
 * The apply runs FIRST: the session locks its persona into the prompt snapshot
 * at the first build, so the files must already hold the preset when the session
 * starts. A failed apply keeps the dialog open — a session created without its
 * preset would silently run the previous persona.
 */
const confirm = async () => {
  const entry = entries.value.find(candidate => candidate.id === selectedId.value);
  if (!entry || creating.value) return;
  creating.value = true;
  try {
    // The tool catalogue comes first: 纯净 / 情感陪伴 derive their agent block
    // from it (a preset applied without it would silently write the wrong set).
    await agentStore.loadCatalog();
    const payload = await loadPresetPayload(entry, locale.value, t, presetCatalogFacts(agentStore));
    // The id is minted first so the apply can also write the preset's agent
    // config (工具 / 中间件 / 子代理模型) into THIS session's registers.
    const sessionId = crypto.randomUUID();
    await applyPresetPayload(payload, sessionId);

    await cacheSessionPreset({ session_id: sessionId, preset_id: entry.id, preset_name: entryName(entry, t) });
    await ensureSessionCharacter(sessionId);

    const now = new Date();
    const pad = (n: number) => String(n).padStart(2, '0');
    const createTime = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
    const meta = { id: sessionId, title: t('history.newSession'), createTime, updatedAt: Date.now() };
    cacheSessionMeta(meta);
    // The left sidebar keeps its own in-memory list; tell it about the new row
    // so the session appears there without a reload.
    emit('session:created', meta);

    visible.value = false;
    // `localePath` (not the named route): the i18n module's localized route
    // variants make a raw named push redirect, which vue-router surfaces as a
    // rejected navigation — the sidebar's own create path does the same.
    await router.push(localePath(`/home/${sessionId}`));
  } catch (e) {
    logUtil.e('[NewSessionPresetDialog] Failed to create the session:', e);
    toastError(t('personaPreset.applyFailed'));
  } finally {
    creating.value = false;
  }
};
</script>

<i18n lang="json">
{
  "en": {
    "personaPreset": {
      "applyFailed": "Applying the preset failed — check the backend connection and retry",
      "create": "Create session",
      "selectHint": "Creating a session requires choosing a preset. The persona and prompt are fixed once the session starts, so later preset changes do not affect existing sessions.",
      "selectTitle": "Choose a preset"
    }
  },
  "zh": {
    "personaPreset": {
      "applyFailed": "预设应用失败，请检查后端连接后重试",
      "create": "创建会话",
      "selectHint": "新建会话需要先选择一个预设；人格与提示词在会话创建后即固定，之后修改预设不影响已创建的会话。",
      "selectTitle": "选择预设"
    }
  },
  "ja": {
    "personaPreset": {
      "applyFailed": "プリセットの適用に失敗しました。バックエンド接続を確認して再試行してください",
      "create": "セッションを作成",
      "selectHint": "新しいセッションの作成にはプリセットの選択が必要です。人格とプロンプトはセッション作成時に固定され、後からプリセットを変更しても既存のセッションには影響しません。",
      "selectTitle": "プリセットを選択"
    }
  },
  "ko": {
    "personaPreset": {
      "applyFailed": "프리셋 적용에 실패했습니다. 백엔드 연결을 확인한 후 다시 시도하세요",
      "create": "세션 만들기",
      "selectHint": "새 세션을 만들려면 프리셋을 선택해야 합니다. 인격과 프롬프트는 세션 생성 시 고정되며, 이후 프리셋 변경은 기존 세션에 영향을 주지 않습니다.",
      "selectTitle": "프리셋 선택"
    }
  }
}
</i18n>
