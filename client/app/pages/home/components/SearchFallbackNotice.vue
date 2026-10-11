<template>
  <!-- Backend-downgrade notice (`search_backend_fallback`): the turn that
       triggered it keeps streaming — this is informational only, so it is a
       dismissible strip, never a modal or a blocking state. -->
  <div
    v-if="notice"
    class="flex items-start gap-2 border-b border-solid border-amber-300/70 bg-amber-50/70 px-3 py-1.5 text-xs text-amber-700 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300"
    data-test="search-fallback-banner"
    role="status">
    <i class="pi pi-exclamation-triangle mt-0.5 shrink-0"></i>
    <span class="min-w-0 flex-1 break-words">
      {{ t('searchFallback.notice', { reason: reasonLabel, name: backendLabel }) }}
    </span>
    <button
      type="button"
      class="shrink-0 rounded p-0.5 hover:bg-amber-100/70 dark:hover:bg-amber-500/20"
      :title="t('searchFallback.dismiss')"
      :aria-label="t('searchFallback.dismiss')"
      data-test="search-fallback-dismiss"
      @click="store.dismissFallback(sessionId)">
      <i class="pi pi-times text-[10px]"></i>
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';

const props = defineProps<{
  /** Session the notice belongs to (the page's `sid`). */
  sessionId: string;
}>();

const { t } = useI18n({ useScope: 'local' });

/** Web-search store: holds the undismissed notice per session. */
const store = useWebSearchStore();

/** The session's undismissed downgrade notice, or null. */
const notice = computed(() => store.fallbackFor(props.sessionId));

/** Display name of the backend now serving searches. */
const backendLabel = computed(() => (notice.value?.backend === 'searxng' ? 'SearXNG' : 'Tavily'));

/**
 * The reason as prose: the three codes the tool sends are translated, anything
 * else (a future reason) is shown verbatim rather than swallowed.
 */
const reasonLabel = computed(() => {
  const reason = notice.value?.reason ?? '';
  const known = ['tavily_auth_failed', 'tavily_timeout', 'tavily_error'] as const;
  return (known as readonly string[]).includes(reason) ? t(`searchFallback.reasons.${reason}`) : reason;
});

// The listener that turns frames into notices is registered once per page; the
// banner mounts with the session page, so this is the earliest safe point.
onMounted(() => store.subscribe());
</script>

<i18n lang="json">
{
  "zh": {
    "searchFallback": {
      "notice": "Tavily 无法使用（{reason}），已切换到 {name}",
      "reasons": {
        "tavily_auth_failed": "密钥无效或已过期",
        "tavily_timeout": "请求超时",
        "tavily_error": "请求失败"
      },
      "dismiss": "关闭通知"
    }
  },
  "en": {
    "searchFallback": {
      "notice": "Tavily is unavailable ({reason}); switched to {name}",
      "reasons": {
        "tavily_auth_failed": "the key is invalid or expired",
        "tavily_timeout": "the request timed out",
        "tavily_error": "the request failed"
      },
      "dismiss": "Dismiss the notice"
    }
  },
  "ja": {
    "searchFallback": {
      "notice": "Tavily を利用できません（{reason}）。{name} に切り替えました",
      "reasons": {
        "tavily_auth_failed": "キーが無効か期限切れ",
        "tavily_timeout": "リクエストがタイムアウト",
        "tavily_error": "リクエストが失敗"
      },
      "dismiss": "通知を閉じる"
    }
  },
  "ko": {
    "searchFallback": {
      "notice": "Tavily를 사용할 수 없어({reason}) {name}(으)로 전환했습니다",
      "reasons": {
        "tavily_auth_failed": "키가 유효하지 않거나 만료됨",
        "tavily_timeout": "요청 시간 초과",
        "tavily_error": "요청 실패"
      },
      "dismiss": "알림 닫기"
    }
  }
}
</i18n>
