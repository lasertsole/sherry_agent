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

        <!-- Where the cap in force comes from: the app-config default for a
             session that never set one, or this session's own choice. -->
        <small
          class="text-xs text-gray-400 dark:text-gray-500"
          data-test="budget-source">
          {{ state?.inherited ? t('budget.inheritHint', { cap: defaultCapLabel }) : t('budget.ownHint') }}
        </small>

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
      </div>

      <div class="flex flex-col gap-3 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
            {{ t('budget.turnTitle') }}
          </span>
          <span class="flex items-center gap-1">
            <Tag
              v-if="state?.turn_pending"
              :value="t('budget.turnPending')"
              severity="warn" />
            <Tag
              v-if="state?.turn_limited"
              :value="t('budget.turnLimited')"
              severity="danger" />
          </span>
        </div>

        <p class="m-0 text-xs text-gray-500 dark:text-gray-400">
          {{ t('budget.turnHint') }}
        </p>

        <div class="flex items-center gap-2">
          <ToggleSwitch
            v-model="turnUnlimited"
            :aria-label="t('budget.turnUnlimited')" />
          <span class="text-sm text-gray-600 dark:text-gray-300">{{ t('budget.turnUnlimited') }}</span>
        </div>

        <small
          class="text-xs text-gray-400 dark:text-gray-500"
          data-test="turn-source">
          {{
            state?.inherited_turn ? t('budget.turnInheritHint', { cap: defaultTurnCapLabel }) : t('budget.turnOwnHint')
          }}
        </small>

        <div
          v-if="!turnUnlimited"
          class="flex flex-col gap-1">
          <label class="text-xs text-gray-500 dark:text-gray-400">{{ t('budget.turnCap') }}</label>
          <InputNumber
            v-model="turnCapInput"
            :min="1"
            :use-grouping="false"
            class="w-full"
            :placeholder="t('budget.capPlaceholder')" />
          <small class="text-xs text-gray-400">{{ t('budget.turnCapHint') }}</small>
        </div>

        <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
          <span>{{ t('budget.turnUsed', { used: formatTokens(state?.turn_used ?? 0), cap: turnCapLabel }) }}</span>
          <span>{{
            t('budget.turnSplit', {
              main: formatTokens(state?.turn_main_used ?? 0),
              child: formatTokens(state?.turn_child_used ?? 0)
            })
          }}</span>
          <span>{{ t('budget.turnCost', { cost: formatCost(state?.turn_cost ?? 0) }) }}</span>
        </div>

        <ProgressBar
          v-if="state?.turn_cap"
          :value="turnPercent"
          :show-value="false"
          class="h-1.5" />
      </div>

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
const turnUnlimited = ref(true);
const turnCapInput = ref<number | null>(null);
const originalTurn = ref<{ unlimited: boolean; cap: number | null }>({
  unlimited: true,
  cap: null
});

const capLabel = computed(() =>
  state.value?.cap && state.value.cap > 0 ? formatTokens(state.value.cap) : t('budget.unlimitedShort')
);

const percent = computed(() => {
  const cap = state.value?.cap ?? 0;
  if (cap <= 0) return 0;
  return Math.min(100, Math.round(((state.value?.used ?? 0) / cap) * 100));
});

const turnCapLabel = computed(() =>
  state.value?.turn_cap && state.value.turn_cap > 0 ? formatTokens(state.value.turn_cap) : t('budget.unlimitedShort')
);

const turnPercent = computed(() => {
  const cap = state.value?.turn_cap ?? 0;
  if (cap <= 0) return 0;
  return Math.min(100, Math.round(((state.value?.turn_used ?? 0) / cap) * 100));
});

const dirty = computed(
  () =>
    unlimited.value !== original.value.unlimited ||
    (!unlimited.value && capInput.value !== original.value.cap) ||
    turnUnlimited.value !== originalTurn.value.unlimited ||
    (!turnUnlimited.value && turnCapInput.value !== originalTurn.value.cap)
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

/**
 * Apply a loaded payload to the edit fields and the dirty baseline.
 *
 * The form edits the SESSION's own choice (``session_cap``): null = it inherits
 * the app-config default (that cap is what the source hint shows), 0 =
 * explicitly unlimited, >0 = a cap of its own.
 * @param next
 */
const applyState = (next: TokenBudgetState) => {
  state.value = next;
  const own = next.session_cap;
  const isUnlimited = own === 0 || (own === null && next.cap === null);
  unlimited.value = isUnlimited;
  capInput.value = isUnlimited ? null : (own ?? next.cap);
  original.value = { unlimited: isUnlimited, cap: capInput.value };

  const ownTurn = next.session_turn_cap;
  const turnIsUnlimited = ownTurn === 0 || (ownTurn === null && next.turn_cap === null);
  turnUnlimited.value = turnIsUnlimited;
  turnCapInput.value = turnIsUnlimited ? null : (ownTurn ?? next.turn_cap);
  originalTurn.value = { unlimited: turnIsUnlimited, cap: turnCapInput.value };
};

/** The app-config default as the source hint shows it (0 → unlimited). */
const defaultCapLabel = computed(() =>
  (state.value?.default_cap ?? 0) > 0 ? formatTokens(state.value!.default_cap) : t('budget.unlimitedShort')
);

/** The app-config per-turn default as the source hint shows it (0 → unlimited). */
const defaultTurnCapLabel = computed(() =>
  (state.value?.default_turn_cap ?? 0) > 0 ? formatTokens(state.value!.default_turn_cap) : t('budget.unlimitedShort')
);

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

/**
 * Persist the session's own cap; a mid-turn change is parked for the next turn.
 *
 * Unlimited (the toggle, or an EMPTIED cap field) writes an explicit ``0``:
 * "unlimited even when the app config carries a default". The toggle is the
 * explicit act; clearing the field means the same thing.
 */
const saveCap = async () => {
  if (!sessionId || busy.value) return;
  let cap = 0;
  if (!unlimited.value) {
    const value = capInput.value;
    if (value === null) {
      unlimited.value = true;
    } else if (!Number.isInteger(value) || value <= 0) {
      loadError.value = t('budget.invalidCap');
      return;
    } else {
      cap = value;
    }
  }
  let turnCap = 0;
  if (!turnUnlimited.value) {
    const value = turnCapInput.value;
    if (value === null) {
      turnUnlimited.value = true;
    } else if (!Number.isInteger(value) || value <= 0) {
      loadError.value = t('budget.invalidTurnCap');
      return;
    } else {
      turnCap = value;
    }
  }
  busy.value = true;
  loadError.value = '';
  try {
    await setTokenBudgetCap(sessionId, cap, turnCap);
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
      "inheritHint": "沿用应用配置的默认上限：{cap}",
      "ownHint": "本会话单独设置（不沿用应用配置）",
      "capHint": "按累计输入 + 输出 token 计算，跨回合累计；留空保存即不限制；修改在下一回合生效。",
      "used": "已用 {used} / {cap}",
      "cost": "估算费用 {cost}",
      "reset": "重置用量",
      "save": "保存",
      "loadError": "读取 Token 预算失败",
      "saveError": "保存 Token 预算失败",
      "resetError": "重置用量失败",
      "invalidCap": "请输入大于 0 的整数",
      "turnTitle": "每回合 Token 上限",
      "turnHint": "统计主代理与本次回合派生的所有子代理；达到上限后本回合停止模型调用。",
      "turnUnlimited": "不限制每回合用量",
      "turnCap": "每回合上限",
      "turnCapHint": "主代理 + 所有子代理的输入 + 输出 token；达到后停止本回合。",
      "turnUsed": "本回合 {used} / {cap}",
      "turnSplit": "主代理 {main} · 子代理 {child}",
      "turnCost": "本回合估算费用 {cost}",
      "turnInheritHint": "沿用应用配置每回合上限：{cap}",
      "turnOwnHint": "本会话单独设置每回合上限",
      "turnPending": "下回合生效",
      "turnLimited": "本回合已达上限",
      "invalidTurnCap": "每回合上限必须是大于 0 的整数"
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
      "inheritHint": "Following the app-config default cap: {cap}",
      "ownHint": "Set for this conversation (the app default does not apply)",
      "capHint": "Counts cumulative input + output tokens across turns; leave it empty to save as unlimited; changes apply from the next turn.",
      "used": "Used {used} / {cap}",
      "cost": "Estimated cost {cost}",
      "reset": "Reset usage",
      "save": "Save",
      "loadError": "Failed to read the token budget",
      "saveError": "Failed to save the token budget",
      "resetError": "Failed to reset the usage",
      "invalidCap": "Enter an integer greater than 0",
      "turnTitle": "Per-turn token budget",
      "turnHint": "Counts the main agent plus every subagent spawned in this turn; the turn stops when the cap is reached.",
      "turnUnlimited": "Unlimited per turn",
      "turnCap": "Per-turn cap",
      "turnCapHint": "Main agent + all subagents, input + output tokens; stops the current turn when reached.",
      "turnUsed": "This turn {used} / {cap}",
      "turnSplit": "Main {main} · Subagents {child}",
      "turnCost": "This turn's estimated cost {cost}",
      "turnInheritHint": "Following the app-config per-turn default: {cap}",
      "turnOwnHint": "Set for this conversation (the app default does not apply)",
      "turnPending": "Applies next turn",
      "turnLimited": "This turn hit the cap",
      "invalidTurnCap": "The per-turn cap must be an integer greater than 0"
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
      "inheritHint": "アプリ設定の既定上限に従います: {cap}",
      "ownHint": "このセッション専用の設定です（アプリ設定の既定は適用されません）",
      "capHint": "累積の入力 + 出力トークンで数えます。空欄で保存すると無制限。変更は次のターンから反映されます。",
      "used": "使用済み {used} / {cap}",
      "cost": "推定費用 {cost}",
      "reset": "使用量をリセット",
      "save": "保存",
      "loadError": "Token 予算の読み取りに失敗しました",
      "saveError": "Token 予算の保存に失敗しました",
      "resetError": "使用量のリセットに失敗しました",
      "invalidCap": "0 より大きい整数を入力してください",
      "turnTitle": "ターンごとの Token 上限",
      "turnHint": "メインエージェントとこのターンで生成したすべてのサブエージェントを集計します。上限に達するとターンを停止します。",
      "turnUnlimited": "ターンごとの上限なし",
      "turnCap": "ターン上限",
      "turnCapHint": "メイン + すべてのサブエージェントの入力 + 出力トークン。到達すると現在のターンを停止します。",
      "turnUsed": "今回のターン {used} / {cap}",
      "turnSplit": "メイン {main} · サブ {child}",
      "turnCost": "今回のターンの推定費用 {cost}",
      "turnInheritHint": "アプリ設定のターン既定上限に従います: {cap}",
      "turnOwnHint": "このセッション専用のターン上限です（アプリ既定は適用されません）",
      "turnPending": "次のターンから適用",
      "turnLimited": "このターンは上限に到達",
      "invalidTurnCap": "ターン上限は 0 より大きい整数で入力してください"
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
      "inheritHint": "앱 설정의 기본 상한을 따릅니다: {cap}",
      "ownHint": "이 세션에만 적용되는 설정입니다(앱 기본값은 적용되지 않음)",
      "capHint": "누적 입력 + 출력 토큰으로 계산합니다. 비워 두고 저장하면 무제한이며, 변경은 다음 턴부터 적용됩니다.",
      "used": "사용 {used} / {cap}",
      "cost": "예상 비용 {cost}",
      "reset": "사용량 초기화",
      "save": "저장",
      "loadError": "Token 예산을 읽지 못했습니다",
      "saveError": "Token 예산을 저장하지 못했습니다",
      "resetError": "사용량을 초기화하지 못했습니다",
      "invalidCap": "0보다 큰 정수를 입력하세요",
      "turnTitle": "턴당 Token 상한",
      "turnHint": "메인 에이전트와 이번 턴에 생성된 모든 서브에이전트를 합산합니다. 상한에 도달하면 턴을 중지합니다.",
      "turnUnlimited": "턴당 무제한",
      "turnCap": "턴당 상한",
      "turnCapHint": "메인 + 모든 서브에이전트의 입력 + 출력 토큰. 도달하면 현재 턴을 중지합니다.",
      "turnUsed": "이번 턴 {used} / {cap}",
      "turnSplit": "메인 {main} · 서브 {child}",
      "turnCost": "이번 턴 예상 비용 {cost}",
      "turnInheritHint": "앱 설정의 턴 기본 상한을 따릅니다: {cap}",
      "turnOwnHint": "이 세션에만 적용되는 턴 상한입니다(앱 기본값은 적용되지 않음)",
      "turnPending": "다음 턴부터 적용",
      "turnLimited": "이번 턴 상한 도달",
      "invalidTurnCap": "턴 상한은 0보다 큰 정수를 입력하세요"
    }
  }
}
</i18n>
