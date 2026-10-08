<template>
  <!-- Right-sidebar tab body: the heartbeat / cron completion notifications. All
       state lives in the notification store (the unread badge keeps counting
       while this tab is closed), so the panel only renders and clears. -->
  <div
    class="flex h-full min-h-0 flex-col gap-2 p-4"
    data-test="notification-panel">
    <div class="min-h-0 flex-1 overflow-y-auto">
      <div
        v-if="store.list.length === 0"
        class="flex h-full flex-col items-center justify-center gap-2 text-center text-sm text-gray-400"
        data-test="notification-empty">
        <i class="pi pi-bell text-2xl opacity-60" />
        <span>{{ t('notification.empty') }}</span>
      </div>

      <div
        v-else
        class="flex flex-col gap-2">
        <div
          v-for="(item, index) in store.list"
          :key="index"
          class="flex items-start gap-2.5 rounded-lg border p-3"
          :class="
            item.source === 'heartbeat'
              ? 'border-sky-200 bg-sky-50/60 dark:border-sky-800/60 dark:bg-sky-900/20'
              : 'border-amber-200 bg-amber-50/60 dark:border-amber-800/60 dark:bg-amber-900/20'
          "
          data-test="notification-item">
          <i
            :class="[
              'pi mt-0.5 text-sm',
              item.source === 'heartbeat' ? 'pi-heart text-sky-500' : 'pi-calendar-clock text-amber-500'
            ]" />
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <span class="text-xs font-medium text-gray-500 dark:text-gray-400">
                {{ t(`notification.source.${item.source}`) }}
              </span>
              <span
                v-if="item.count > 1"
                class="rounded-full bg-red-500 px-1.5 py-0.5 text-[10px] leading-none text-white">
                {{ t('notification.merge.count', { count: item.count }) }}
              </span>
            </div>
            <p class="mt-1 whitespace-pre-wrap break-all text-sm text-gray-800 dark:text-gray-200">
              {{ item.content }}
            </p>
            <p class="mt-1 text-xs text-gray-400">
              {{ item.time }}
            </p>
          </div>
        </div>
      </div>
    </div>

    <div
      v-if="store.list.length > 0"
      class="flex shrink-0 justify-end border-t border-solid border-gray-200 pt-2 dark:border-gray-700">
      <Button
        icon="pi pi-trash"
        :label="t('notification.clear')"
        size="small"
        severity="secondary"
        data-test="notification-clear"
        @click="store.clearAll()" />
    </div>
  </div>
</template>

<script lang="ts" setup>
import { onBeforeUnmount, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';

const { t } = useI18n({ useScope: 'local' });

/** Shared notification state (the same store the top-bar badge reads). */
const store = useNotificationStore();
store.subscribe();

// While this tab is on screen a push does not re-flag the list, and mounting it
// counts as reading: the badge zeroes as soon as the tab opens.
onMounted(() => store.setPanelVisible(true));
onBeforeUnmount(() => store.setPanelVisible(false));
</script>

<i18n lang="json">
{
  "zh": {
    "notification": {
      "empty": "暂无通知。当心跳或定时任务完成时，通知会显示在这里。",
      "clear": "清空",
      "source": {
        "heartbeat": "心跳任务",
        "cron": "定时任务"
      },
      "merge": {
        "count": "{count} ×"
      }
    }
  },
  "en": {
    "notification": {
      "empty": "No notifications yet. Notifications will appear here when heartbeat or scheduled tasks complete.",
      "clear": "Clear",
      "source": {
        "heartbeat": "Heartbeat Task",
        "cron": "Scheduled Task"
      },
      "merge": {
        "count": "{count} ×"
      }
    }
  },
  "ja": {
    "notification": {
      "empty": "通知はまだありません。ハートビートまたは定期タスクの完了時にここに表示されます。",
      "clear": "クリア",
      "source": {
        "heartbeat": "ハートビートタスク",
        "cron": "定期タスク"
      },
      "merge": {
        "count": "{count} ×"
      }
    }
  },
  "ko": {
    "notification": {
      "empty": "알림이 없습니다. 하트비트 또는 정기 태스크가 완료되면 여기에 표시됩니다.",
      "clear": "지우기",
      "source": {
        "heartbeat": "하트비트 태스크",
        "cron": "정기 태스크"
      },
      "merge": {
        "count": "{count} ×"
      }
    }
  }
}
</i18n>
