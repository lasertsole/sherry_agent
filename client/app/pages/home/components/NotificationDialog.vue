<i18n lang="json">
{
  "en": {
    "notification": {
      "title": "Notifications",
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
      "title": "通知",
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
      "title": "알림",
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
  },
  "zh": {
    "notification": {
      "title": "通知",
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
  }
}
</i18n>

<template>
  <Dialog
    v-model:visible="visible"
    :header="t('notification.title')"
    :modal="true"
    :closable="true"
    class="w-[min(95vw,1200px)]">
    <div class="flex flex-col gap-2">
      <div
        class="overflow-y-auto"
        style="max-height: 60vh">
        <div
          v-if="list.length === 0"
          class="flex flex-col items-center justify-center gap-2 py-8 text-center text-sm text-gray-400">
          <i class="pi pi-bell text-2xl opacity-60" />
          <span>{{ t('notification.empty') }}</span>
        </div>

        <div
          v-else
          class="flex flex-col gap-2">
          <div
            v-for="(item, index) in list"
            :key="index"
            class="flex items-start gap-2.5 rounded-lg border p-3"
            :class="
              item.source === 'heartbeat'
                ? 'border-sky-200 bg-sky-50/60 dark:border-sky-800/60 dark:bg-sky-900/20'
                : 'border-amber-200 bg-amber-50/60 dark:border-amber-800/60 dark:bg-amber-900/20'
            ">
            <i
              :class="[
                'pi mt-0.5 text-sm',
                item.source === 'heartbeat' ? 'pi-heart text-sky-500' : 'pi-calendar-clock text-amber-500'
              ]" />
            <div class="flex-1 min-w-0">
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
        v-if="list.length > 0"
        class="flex justify-end border-t border-gray-200 dark:border-gray-700 pt-2">
        <Button
          icon="pi pi-trash"
          :label="t('notification.clear')"
          size="small"
          severity="secondary"
          @click="clearAll" />
      </div>
    </div>
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useWs } from '@/composables/ws';
import { on } from '@/composables/mitt';
import dayjs from 'dayjs';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; changed: [unread: number] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => {
    emits('update:modelValue', v);
    if (v) clearUnread();
  }
});

// When the parent opens the dialog via v-model, the computed setter does not trigger;
// this watches modelValue to make sure the unread badge is also cleared when the parent opens the dialog.
watch(
  () => props.modelValue,
  open => {
    if (open) clearUnread();
  }
);

/** A single notification (merged item) */
interface NotificationItem {
  source: 'heartbeat' | 'cron';
  content: string;
  count: number;
  time: string; // time of first occurrence
}

/** All notifications (including the unread count) */
const items = ref<NotificationItem[]>([]);

/** List displayed newest → oldest */
const list = computed<NotificationItem[]>(() => [...items.value].reverse());

/** Whether it has been read (counts as read once the dialog has been opened) */
const read = ref(false);

/** Badge unread count: 0 when read */
const unreadCount = computed<number>(() => (read.value ? 0 : items.value.reduce((sum, i) => sum + i.count, 0)));

/** Clear the unread state when the dialog opens (content is kept, only the badge is zeroed) */
const clearUnread = () => {
  read.value = true;
  emits('changed', 0);
};

/** Clear all notifications */
const clearAll = () => {
  items.value = [];
  emits('changed', 0);
};

/** Notification content (WS-pushed content may be an object or a string) */
const contentOf = (payload: unknown): string => {
  if (payload == null) return '';
  if (typeof payload === 'string') return payload.trim();
  try {
    const s = JSON.stringify(payload);
    return s && s !== '{}' ? s : '';
  } catch {
    return '';
  }
};

/** Determine the notification source (whether the content contains task keywords) */
const sourceOf = (content: string): NotificationItem['source'] => {
  // The server includes a source marker prefix in content; when no marker exists, fall back to content keywords
  if (/^(heartbeat|cron):\s*/.test(content)) return content.startsWith('heartbeat:') ? 'heartbeat' : 'cron';
  return /\b(cron|定时|定時)\b/i.test(content) ? 'cron' : 'heartbeat';
};

/** Handle one notification: merge the count when identical to the "previous (latest)" item */
const handleNotification = (payload: unknown) => {
  const content = contentOf(payload);
  if (!content) return;

  const latest = items.value.length > 0 ? items.value[items.value.length - 1] : null;
  if (latest && latest.content === content) {
    latest.count += 1;
  } else {
    items.value.push({
      source: sourceOf(content),
      content,
      count: 1,
      time: dayjs().format('HH:mm:ss')
    });
  }

  // Reset to unread when a new notification arrives (if the dialog is not open)
  if (!visible.value) {
    read.value = false;
  }
  emits('changed', unreadCount.value);
};

// Establish the WS notification subscription
useWs();
on('ws:notification', handleNotification);

// Notify the parent component of the initial unread count
emits('changed', 0);
</script>
