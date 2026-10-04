<template>
  <!-- Two steps, deliberately: first the per-file plan (a dry run, nothing is
       touched), then the destructive confirmation. A refusal is shown with its
       reasons and, inside a git work tree, the three-way-merge hint the server
       computed — read-only information, never an automatic merge. -->
  <div
    v-if="visible"
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    @click.self="$emit('close')">
    <div
      class="w-full max-w-lg overflow-hidden rounded-xl bg-white shadow-xl dark:bg-gray-900"
      role="dialog"
      aria-modal="true">
      <header class="border-b border-gray-100 px-4 py-3 text-sm font-medium dark:border-gray-800">
        {{ t('title') }}
      </header>

      <div class="max-h-80 overflow-y-auto px-4 py-3 text-sm">
        <p
          v-if="plan === null"
          class="text-gray-500">
          {{ t('planning') }}
        </p>
        <template v-else>
          <p
            v-if="plan.success"
            class="mb-2 text-gray-600 dark:text-gray-300">
            {{ t('planReady', { count: plan.files.length }) }}
          </p>
          <p
            v-else
            class="mb-2 text-red-600 dark:text-red-400">
            {{ plan.error }}<template v-if="plan.hint"> — {{ plan.hint }}</template>
          </p>
          <ul class="space-y-1">
            <li
              v-for="file in plan.files"
              :key="file.path"
              class="flex items-start gap-2 font-mono text-xs">
              <span :class="file.safe ? 'text-emerald-600' : 'text-red-500'">
                {{ file.safe ? '✓' : '✕' }}
              </span>
              <span class="break-all">{{ file.path }}</span>
              <span class="ml-auto shrink-0 text-gray-400">
                {{ file.reason || file.action }}
              </span>
            </li>
          </ul>
          <p
            v-if="plan.merge3way"
            class="mt-3 rounded bg-gray-50 p-2 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">
            {{ t('mergeHint', { command: plan.merge3way.command, note: plan.merge3way.note }) }}
          </p>
        </template>
      </div>

      <footer class="flex justify-end gap-2 border-t border-gray-100 px-4 py-3 dark:border-gray-800">
        <button
          type="button"
          class="rounded-md px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
          @click="$emit('close')">
          {{ t('cancel') }}
        </button>
        <button
          type="button"
          :disabled="plan === null || !plan.success || applying"
          :class="[
            'rounded-md px-3 py-1.5 text-sm text-white',
            plan !== null && plan.success && !applying
              ? 'bg-red-600 hover:bg-red-700'
              : 'cursor-not-allowed bg-gray-300 dark:bg-gray-700'
          ]"
          @click="$emit('confirm')">
          {{ applying ? t('applying') : t('confirm') }}
        </button>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';

defineProps<{
  /** Whether the dialog is open */
  visible: boolean;
  /** The dry-run plan (null while it loads) */
  plan: {
    success: boolean;
    error?: string;
    hint?: string;
    files: Array<{ path: string; action: string; safe: boolean; reason?: string }>;
    merge3way?: { command: string; note: string } | null;
  } | null;
  /** Whether the destructive step is in flight */
  applying: boolean;
}>();
defineEmits<{ close: []; confirm: [] }>();

const { t } = useI18n();
</script>

<i18n lang="json">
{
  "en": {
    "title": "Undo file changes",
    "planning": "Checking what can still be reverted…",
    "planReady": "{count} file(s) would be restored to the snapshotted content.",
    "mergeHint": "A three-way merge may help (based on the state at check time): {command} — {note}",
    "cancel": "Cancel",
    "confirm": "Undo",
    "applying": "Undoing…"
  },
  "zh": {
    "title": "撤销文件改动",
    "planning": "正在检查可撤销的内容…",
    "planReady": "将把 {count} 个文件还原为快照内容。",
    "mergeHint": "可尝试三方合并（基于检查时刻的状态）：{command} — {note}",
    "cancel": "取消",
    "confirm": "撤销",
    "applying": "撤销中…"
  },
  "ja": {
    "title": "ファイル変更を元に戻す",
    "planning": "元に戻せる内容を確認しています…",
    "planReady": "{count} 個のファイルをスナップショットの内容に戻します。",
    "mergeHint": "三方マージが有効な場合があります（確認時点の状態に基づく）: {command} — {note}",
    "cancel": "キャンセル",
    "confirm": "元に戻す",
    "applying": "実行中…"
  },
  "ko": {
    "title": "파일 변경 취소",
    "planning": "되돌릴 수 있는 항목을 확인하는 중…",
    "planReady": "{count}개 파일을 스냅샷 내용으로 되돌립니다.",
    "mergeHint": "삼방 병합이 도움이 될 수 있습니다(확인 시점 상태 기준): {command} — {note}",
    "cancel": "취소",
    "confirm": "되돌리기",
    "applying": "되돌리는 중…"
  }
}
</i18n>
