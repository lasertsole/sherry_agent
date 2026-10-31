<template>
  <div class="flex flex-col gap-4">
    <p class="m-0 text-xs text-gray-500 dark:text-gray-400">
      {{ t('budget.hint') }}
    </p>

    <div
      v-if="!sessionId"
      class="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-4 text-sm text-gray-400">
      {{ t('budget.noSession') }}
    </div>

    <template v-else>
      <p
        v-if="loadError"
        class="m-0 text-sm text-red-600 dark:text-red-400">
        {{ loadError }}
      </p>

      <div class="flex flex-col gap-3 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
            {{ t('budget.title') }}
          </span>
          <span class="flex items-center gap-1">
            <Tag
              v-if="state?.pending"
              :value="t('budget.pending')"
              severity="warn" />
            <Tag
              v-if="state?.limited"
              :value="t('budget.limited')"
              severity="danger" />
          </span>
        </div>

        <div class="flex items-center gap-2">
          <ToggleSwitch
            v-model="unlimited"
            :aria-label="t('budget.unlimited')" />
          <span class="text-sm text-gray-600 dark:text-gray-300">{{ t('budget.unlimited') }}</span>
        </div>

        <div
          v-if="!unlimited"
          class="flex flex-col gap-1">
          <label class="text-xs text-gray-500 dark:text-gray-400">{{ t('budget.cap') }}</label>
          <InputNumber
            v-model="capInput"
            :min="1"
            :use-grouping="false"
            class="w-full"
            :placeholder="t('budget.capPlaceholder')" />
          <small class="text-xs text-gray-400">{{ t('budget.capHint') }}</small>
        </div>

        <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
          <span>{{ t('budget.used', { used: formatTokens(state?.used ?? 0), cap: capLabel }) }}</span>
          <span>{{ t('budget.cost', { cost: formatCost(state?.cost ?? 0) }) }}</span>
        </div>

        <ProgressBar
          v-if="state?.cap"
          :value="percent"
          :show-value="false"
          class="h-1.5" />

        <div class="flex justify-end gap-2">
          <Button
            :label="t('budget.reset')"
            icon="pi pi-refresh"
            severity="secondary"
            size="small"
            :loading="busy"
            @click="resetUsage" />
          <Button
            :label="t('budget.save')"
            icon="pi pi-check"
            size="small"
            :loading="busy"
            :disabled="!dirty"
            @click="saveCap" />
        </div>
      </div>
    </template>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
// The bridge needs a stable module specifier so tests can vi.mock it (the
// unimport injection is compile-time and leaves bare symbols unmockable), the
// same exception `stores/context-usage.ts` carries.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import {
  fetchTokenBudget,
  resetTokenBudgetUsage,
  setTokenBudgetCap,
  type TokenBudgetState
} from '~/composables/bridge/session';
import { logUtil } from '~/utils/log';

/**
 * The session this budget belongs to. Mounted as a session-scoped right-sidebar
 * tab, so it arrives in the tab payload (the way ContextViewerPanel takes its
 * session) rather than as a bare prop.
 */
const props = defineProps<{ payload?: { sessionId?: string } }>();
/** The owning session id ('' when the tab was opened without one). */
const sessionId = String(props.payload?.sessionId || '');
const { t } = useI18n({ useScope: 'local' });

const state = ref<TokenBudgetState | null>(null);
const unlimited = ref(true);
const capInput = ref<number | null>(null);
const busy = ref(false);
const loadError = ref('');

/** The cap the panel loaded, used to enable the Save button only on a real edit. */
const original = ref<{ unlimited: boolean; cap: number | null }>({ unlimited: true, cap: null });

const capLabel = computed(() =>
  state.value?.cap && state.value.cap > 0 ? formatTokens(state.value.cap) : t('budget.unlimitedShort')
);

const percent = computed(() => {
  const cap = state.value?.cap ?? 0;
  if (cap <= 0) return 0;
  return Math.min(100, Math.round(((state.value?.used ?? 0) / cap) * 100));
});

const dirty = computed(
  () => unlimited.value !== original.value.unlimited || (!unlimited.value && capInput.value !== original.value.cap)
);

/**
 * Format a token count with the active locale's group separators.
 * @param value
 */
function formatTokens(value: number): string {
  return Math.max(0, Math.round(value)).toLocaleString();
}

/**
 * Format the estimated spend (the backend reports USD).
 * @param value
 */
function formatCost(value: number): string {
  return `$${Math.max(0, value).toFixed(4)}`;
}

/** Apply a loaded payload to the edit fields and the dirty baseline. */
const applyState = (next: TokenBudgetState) => {
  state.value = next;
  const isUnlimited = next.cap === null || next.cap <= 0;
  unlimited.value = isUnlimited;
  capInput.value = isUnlimited ? null : next.cap;
  original.value = { unlimited: isUnlimited, cap: capInput.value };
};

/** Load the current session's budget (the panel can be opened before a session). */
const load = async () => {
  loadError.value = '';
  if (!sessionId) {
    state.value = null;
    return;
  }
  try {
    applyState(await fetchTokenBudget(sessionId));
  } catch (e) {
    logUtil.e('[TokenBudgetPanel] Failed to load budget:', e);
    loadError.value = t('budget.loadError');
  }
};

/** Persist the cap (null = unlimited); a mid-turn change is parked for the next turn. */
const saveCap = async () => {
  if (!sessionId || busy.value) return;
  let cap: number | null = null;
  if (!unlimited.value) {
    const value = capInput.value;
    if (value === null || !Number.isInteger(value) || value <= 0) {
      loadError.value = t('budget.invalidCap');
      return;
    }
    cap = value;
  }
  busy.value = true;
  loadError.value = '';
  try {
    await setTokenBudgetCap(sessionId, cap);
    await load();
  } catch (e) {
    logUtil.e('[TokenBudgetPanel] Failed to save budget:', e);
    loadError.value = t('budget.saveError');
  } finally {
    busy.value = false;
  }
};

/** Clear USED/COST/LIMITED, preserving the cap. */
const resetUsage = async () => {
  if (!sessionId || busy.value) return;
  busy.value = true;
  loadError.value = '';
  try {
    applyState(await resetTokenBudgetUsage(sessionId));
  } catch (e) {
    logUtil.e('[TokenBudgetPanel] Failed to reset budget usage:', e);
    loadError.value = t('budget.resetError');
  } finally {
    busy.value = false;
  }
};

// The tab is per session (a session-scoped sidebar tab), so the id is fixed for
// this mount: load once.
onMounted(() => {
  void load();
});
</script>

<i18n lang="json">
{
  "zh": {
    "budget": {
      "hint": "限制当前会话累计消耗的 token 数；达到上限后，模型调用会直接返回预算耗尽提示。",
      "noSession": "请先打开一个会话；Token 预算按会话生效。",
      "title": "当前会话 Token 预算",
      "pending": "下一回合生效",
      "limited": "已达上限",
      "unlimited": "不限制",
      "unlimitedShort": "不限制",
      "cap": "Token 上限",
      "capPlaceholder": "例如 500000",
      "capHint": "按累计输入 + 输出 token 计算，跨回合累计；修改在下一回合生效。",
      "used": "已用 {used} / {cap}",
      "cost": "估算费用 {cost}",
      "reset": "重置用量",
      "save": "保存",
      "loadError": "读取 Token 预算失败",
      "saveError": "保存 Token 预算失败",
      "resetError": "重置用量失败",
      "invalidCap": "请输入大于 0 的整数"
    }
  },
  "en": {
    "budget": {
      "hint": "Cap the tokens this session may consume in total; once reached, model calls return a budget-exhausted reply.",
      "noSession": "Open a session first — the Token budget applies per session.",
      "title": "Current session token budget",
      "pending": "Applies next turn",
      "limited": "Limit reached",
      "unlimited": "Unlimited",
      "unlimitedShort": "Unlimited",
      "cap": "Token cap",
      "capPlaceholder": "e.g. 500000",
      "capHint": "Counts cumulative input + output tokens across turns; changes apply from the next turn.",
      "used": "Used {used} / {cap}",
      "cost": "Estimated cost {cost}",
      "reset": "Reset usage",
      "save": "Save",
      "loadError": "Failed to read the token budget",
      "saveError": "Failed to save the token budget",
      "resetError": "Failed to reset the usage",
      "invalidCap": "Enter an integer greater than 0"
    }
  },
  "ja": {
    "budget": {
      "hint": "このセッションが累積で消費できるトークン数を制限します。上限に達すると、モデル呼び出しは予算超過の応答を返します。",
      "noSession": "先にセッションを開いてください。Token 予算はセッション単位で適用されます。",
      "title": "現在のセッションの Token 予算",
      "pending": "次のターンから適用",
      "limited": "上限に到達",
      "unlimited": "無制限",
      "unlimitedShort": "無制限",
      "cap": "Token 上限",
      "capPlaceholder": "例: 500000",
      "capHint": "入力 + 出力トークンの累積で、ターンをまたいで計算されます。変更は次のターンから適用されます。",
      "used": "使用済み {used} / {cap}",
      "cost": "推定費用 {cost}",
      "reset": "使用量をリセット",
      "save": "保存",
      "loadError": "Token 予算の読み取りに失敗しました",
      "saveError": "Token 予算の保存に失敗しました",
      "resetError": "使用量のリセットに失敗しました",
      "invalidCap": "0 より大きい整数を入力してください"
    }
  },
  "ko": {
    "budget": {
      "hint": "이 세션이 누적해서 소비할 수 있는 토큰 수를 제한합니다. 한도에 도달하면 모델 호출은 예산 초과 응답을 반환합니다.",
      "noSession": "먼저 세션을 여세요. Token 예산은 세션 단위로 적용됩니다.",
      "title": "현재 세션 Token 예산",
      "pending": "다음 턴부터 적용",
      "limited": "한도 도달",
      "unlimited": "무제한",
      "unlimitedShort": "무제한",
      "cap": "Token 상한",
      "capPlaceholder": "예: 500000",
      "capHint": "입력 + 출력 토큰의 누적이며 턴을 넘어 계산됩니다. 변경은 다음 턴부터 적용됩니다.",
      "used": "사용 {used} / {cap}",
      "cost": "예상 비용 {cost}",
      "reset": "사용량 초기화",
      "save": "저장",
      "loadError": "Token 예산을 읽지 못했습니다",
      "saveError": "Token 예산을 저장하지 못했습니다",
      "resetError": "사용량을 초기화하지 못했습니다",
      "invalidCap": "0보다 큰 정수를 입력하세요"
    }
  }
}
</i18n>
