<template>
  <div class="flex h-full flex-col gap-4 overflow-y-auto p-1">
    <p class="m-0 text-xs text-gray-500 dark:text-gray-400">
      {{ t('highRiskGuard.hint') }}
    </p>

    <div
      v-if="!sessionId"
      class="rounded-lg border border-dashed border-gray-300 p-4 text-sm text-gray-400 dark:border-gray-700">
      {{ t('highRiskGuard.noSession') }}
    </div>

    <template v-else>
      <p
        v-if="error"
        class="m-0 text-sm text-red-600 dark:text-red-400"
        data-test="high-risk-error">
        {{ error }}
      </p>

      <!-- Keyword registry -->
      <div class="flex items-center justify-between gap-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('highRiskGuard.keywords') }}
        </span>
        <Button
          icon="pi pi-refresh"
          size="small"
          text
          :aria-label="t('highRiskGuard.refresh')"
          :loading="loading"
          @click="load" />
      </div>

      <div class="flex flex-col gap-2">
        <div class="flex gap-2">
          <InputText
            v-model="draft"
            :placeholder="t('highRiskGuard.addPlaceholder')"
            class="flex-1"
            size="small"
            data-test="high-risk-keyword-input"
            @keyup.enter="addKeyword" />
          <Button
            :label="t('highRiskGuard.add')"
            size="small"
            :disabled="!canAdd"
            :loading="busy"
            @click="addKeyword" />
        </div>
        <small
          v-if="draft.length > 0 && draft.trim().length < 3"
          class="text-xs text-red-500">
          {{ t('highRiskGuard.tooShort') }}
        </small>
      </div>

      <div
        v-if="keywords.length === 0"
        class="rounded-lg border border-dashed border-gray-300 p-4 text-center text-sm text-gray-400 dark:border-gray-700">
        {{ t('highRiskGuard.empty') }}
      </div>
      <div
        v-else
        class="flex flex-col gap-2">
        <div
          v-for="entry in keywords"
          :key="`${entry.scope}:${entry.keyword}`"
          class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
          :data-test="`high-risk-keyword-${entry.keyword}`">
          <div class="flex items-center justify-between gap-2">
            <span class="break-all font-mono text-sm text-gray-800 dark:text-gray-100">{{ entry.keyword }}</span>
            <span class="flex shrink-0 items-center gap-1">
              <Tag
                :value="tagLabel(entry)"
                :severity="tagSeverity(entry)" />
              <Button
                v-if="entry.id !== null"
                icon="pi pi-trash"
                size="small"
                text
                severity="danger"
                :aria-label="t('highRiskGuard.delete')"
                @click="removeKeyword(entry)" />
            </span>
          </div>
          <div class="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <span>{{ sourceLabel(entry.source) }}</span>
            <span>·</span>
            <span>{{ entry.scope }}</span>
            <Button
              v-if="entry.executed"
              :label="t('highRiskGuard.reset')"
              size="small"
              text
              @click="resetKeyword(entry.keyword)" />
            <Button
              :label="entry.policy === 'block' ? t('highRiskGuard.allow') : t('highRiskGuard.block')"
              size="small"
              text
              @click="togglePolicy(entry)" />
          </div>
          <div
            v-if="entry.executed && entry.result_summary"
            class="mt-1 break-all text-xs text-gray-400">
            {{ t('highRiskGuard.summary') }}: {{ entry.result_summary }}
          </div>
        </div>
      </div>

      <Divider class="my-2" />

      <!-- Executed-operation ledger -->
      <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
        {{ t('highRiskGuard.ops') }}
      </span>
      <div
        v-if="ops.length === 0"
        class="rounded-lg border border-dashed border-gray-300 p-4 text-center text-sm text-gray-400 dark:border-gray-700">
        {{ t('highRiskGuard.noOps') }}
      </div>
      <div
        v-else
        class="flex flex-col gap-2">
        <div
          v-for="op in ops"
          :key="op.id"
          class="rounded-lg border border-red-100 p-3 dark:border-red-900"
          :data-test="`high-risk-op-${op.id}`">
          <div class="flex items-center justify-between gap-2">
            <span class="break-all font-mono text-sm text-gray-800 dark:text-gray-100">{{ op.tool_name }}</span>
            <Button
              icon="pi pi-trash"
              size="small"
              text
              severity="danger"
              :aria-label="t('highRiskGuard.delete')"
              @click="removeOp(op.id)" />
          </div>
          <div class="mt-1 break-all font-mono text-xs text-gray-500 dark:text-gray-400">{{ op.args_preview }}</div>
          <div class="mt-1 break-all text-xs text-gray-400">
            {{ formatTime(op.executed_at) }} · {{ op.result_summary }}
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';

import type { HighRiskKeyword, HighRiskOp } from '~/composables/highRiskGuard';

/**
 * The session's high-risk guard (shield in the toolbar):
 *
 * - the keyword registry: a `block` keyword makes any tool call whose arguments
 *   contain it ask a human (main) or bubble [NEEDS_APPROVAL] out of a child; an
 *   `allow` keyword is never scanned;
 * - the executed-operation ledger: an exact high-risk call (PTC / MCP) that ran
 *   once is refused on a repeat (the verify-retest guard) until its row is deleted;
 * - a keyword that has EXECUTED is refused on a repeat until its marker is reset.
 *
 * All state lives on the backend store; this panel is the human view over it.
 */
const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ payload?: { sessionId?: string } }>();
const sessionId = String(props.payload?.sessionId || '');

const keywords = ref<HighRiskKeyword[]>([]);
const ops = ref<HighRiskOp[]>([]);
const draft = ref('');
const loading = ref(false);
const busy = ref(false);
const error = ref('');

const canAdd = computed(() => draft.value.trim().length >= 3 && !busy.value);

/**
 * Surface a failed mutation: the backend's own message wins (it names the
 * validation rule), else the caller's generic label.
 * @param result The mutation result.
 * @param fallback The generic label.
 */
function fail(result: { message?: string }, fallback: string): void {
  error.value = result.message || fallback;
}

/** Read the keywords + ledger. */
async function load(): Promise<void> {
  if (!sessionId) return;
  loading.value = true;
  error.value = '';
  try {
    const snapshot = await readHighRiskGuard(sessionId);
    keywords.value = snapshot.keywords;
    ops.value = snapshot.ops;
  } catch {
    error.value = t('highRiskGuard.loadError');
  } finally {
    loading.value = false;
  }
}

/** Register the drafted keyword (session scope) and reload. */
async function addKeyword(): Promise<void> {
  if (!canAdd.value) return;
  busy.value = true;
  error.value = '';
  try {
    const result = await addHighRiskKeyword(sessionId, draft.value.trim());
    if (!result.success) {
      fail(result, t('highRiskGuard.saveError'));
      return;
    }
    draft.value = '';
    await load();
  } catch {
    error.value = t('highRiskGuard.saveError');
  } finally {
    busy.value = false;
  }
}

/** Flip one keyword between block and allow. */
async function togglePolicy(entry: HighRiskKeyword): Promise<void> {
  const policy = entry.policy === 'block' ? 'allow' : 'block';
  const result = await setHighRiskKeywordPolicy(sessionId, entry.keyword, policy);
  if (!result.success) fail(result, t('highRiskGuard.saveError'));
  await load();
}

/** Clear an executed marker (one more run allowed). */
async function resetKeyword(keyword: string): Promise<void> {
  const result = await resetHighRiskKeyword(sessionId, keyword);
  if (!result.success) fail(result, t('highRiskGuard.saveError'));
  await load();
}

/** Delete one keyword row (a built-in override returns to the default). */
async function removeKeyword(entry: HighRiskKeyword): Promise<void> {
  if (entry.id === null) return;
  const result = await removeHighRiskKeyword(sessionId, entry.id);
  if (!result.success) fail(result, t('highRiskGuard.saveError'));
  await load();
}

/** Delete one ledger row (allows an exact repeat). */
async function removeOp(id: number): Promise<void> {
  const result = await removeHighRiskOp(sessionId, id);
  if (!result.success) fail(result, t('highRiskGuard.saveError'));
  await load();
}

function tagLabel(entry: HighRiskKeyword): string {
  if (entry.executed) return t('highRiskGuard.executed');
  return entry.policy === 'allow' ? t('highRiskGuard.allowTag') : t('highRiskGuard.blockTag');
}

function tagSeverity(entry: HighRiskKeyword): 'success' | 'warn' | 'danger' {
  if (entry.executed) return 'warn';
  return entry.policy === 'allow' ? 'success' : 'danger';
}

function sourceLabel(source: string): string {
  if (source === 'builtin') return t('highRiskGuard.sourceBuiltin');
  if (source === 'agent') return t('highRiskGuard.sourceAgent');
  return t('highRiskGuard.sourceUser');
}

function formatTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}

onMounted(() => {
  void load();
});
</script>

<i18n lang="json">
{
  "zh": {
    "highRiskGuard": {
      "title": "高危防护",
      "hint": "高危关键字命中时主智能体会询问你；子智能体会把 [NEEDS_APPROVAL] 冒泡给主智能体。已执行过的操作（PTC / MCP）重复执行会被拒绝，删除账本记录可放行一次。",
      "noSession": "请先打开一个会话。",
      "keywords": "高危关键字",
      "addPlaceholder": "添加关键字（至少 3 个字符）",
      "add": "添加",
      "tooShort": "关键字至少 3 个字符",
      "empty": "暂无关键字",
      "allow": "改为放行",
      "block": "改为拦截",
      "allowTag": "放行",
      "blockTag": "拦截",
      "executed": "已执行",
      "reset": "重置执行标记",
      "summary": "首次结果",
      "ops": "已执行的高危操作",
      "noOps": "暂无记录",
      "delete": "删除",
      "refresh": "刷新",
      "loadError": "读取失败，请检查后端服务",
      "saveError": "保存失败，请检查后端服务",
      "sourceBuiltin": "内置",
      "sourceUser": "用户",
      "sourceAgent": "智能体"
    }
  },
  "en": {
    "highRiskGuard": {
      "title": "High-risk guard",
      "hint": "A high-risk keyword makes the main agent ask you; a subagent bubbles [NEEDS_APPROVAL] to its parent. An executed operation (PTC / MCP) is refused on a repeat — delete the ledger row to allow one more run.",
      "noSession": "Open a session first.",
      "keywords": "High-risk keywords",
      "addPlaceholder": "Add a keyword (at least 3 characters)",
      "add": "Add",
      "tooShort": "A keyword needs at least 3 characters",
      "empty": "No keywords yet",
      "allow": "Allow",
      "block": "Block",
      "allowTag": "allow",
      "blockTag": "block",
      "executed": "executed",
      "reset": "Reset marker",
      "summary": "First result",
      "ops": "Executed high-risk operations",
      "noOps": "Nothing recorded",
      "delete": "Delete",
      "refresh": "Refresh",
      "loadError": "Could not load; check the backend",
      "saveError": "Could not save; check the backend",
      "sourceBuiltin": "built-in",
      "sourceUser": "user",
      "sourceAgent": "agent"
    }
  },
  "ja": {
    "highRiskGuard": {
      "title": "高リスク保護",
      "hint": "高リスクキーワードに一致するとメインエージェントが確認します。サブエージェントは [NEEDS_APPROVAL] を親へバブルします。一度実行した操作（PTC / MCP）の再実行は拒否され、台帳行を削除すると一度だけ許可されます。",
      "noSession": "先にセッションを開いてください。",
      "keywords": "高リスクキーワード",
      "addPlaceholder": "キーワードを追加(3 文字以上)",
      "add": "追加",
      "tooShort": "キーワードは 3 文字以上必要です",
      "empty": "キーワードはまだありません",
      "allow": "許可に変更",
      "block": "ブロックに変更",
      "allowTag": "許可",
      "blockTag": "ブロック",
      "executed": "実行済み",
      "reset": "実行マーカーをリセット",
      "summary": "初回結果",
      "ops": "実行済みの高リスク操作",
      "noOps": "記録はありません",
      "delete": "削除",
      "refresh": "更新",
      "loadError": "読み取りに失敗しました。バックエンドを確認してください",
      "saveError": "保存に失敗しました。バックエンドを確認してください",
      "sourceBuiltin": "組み込み",
      "sourceUser": "ユーザー",
      "sourceAgent": "エージェント"
    }
  },
  "ko": {
    "highRiskGuard": {
      "title": "고위험 보호",
      "hint": "고위험 키워드가 포함되면 메인 에이전트가 사용자에게 확인합니다. 하위 에이전트는 [NEEDS_APPROVAL]을 부모로 버블합니다. 한 번 실행한 작업(PTC / MCP)의 재실행은 거부되며, 원장 행을 삭제하면 한 번 더 허용됩니다.",
      "noSession": "먼저 세션을 여세요.",
      "keywords": "고위험 키워드",
      "addPlaceholder": "키워드 추가(3자 이상)",
      "add": "추가",
      "tooShort": "키워드는 3자 이상이어야 합니다",
      "empty": "아직 키워드가 없습니다",
      "allow": "허용으로 변경",
      "block": "차단으로 변경",
      "allowTag": "허용",
      "blockTag": "차단",
      "executed": "실행됨",
      "reset": "실행 표시 초기화",
      "summary": "첫 결과",
      "ops": "실행된 고위험 작업",
      "noOps": "기록 없음",
      "delete": "삭제",
      "refresh": "새로 고침",
      "loadError": "읽지 못했습니다. 백엔드를 확인하세요",
      "saveError": "저장하지 못했습니다. 백엔드를 확인하세요",
      "sourceBuiltin": "내장",
      "sourceUser": "사용자",
      "sourceAgent": "에이전트"
    }
  }
}
</i18n>
