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
      <div class="overflow-y-auto" style="max-height: 60vh;">
        <div v-if="list.length === 0" class="flex flex-col items-center justify-center gap-2 py-8 text-center text-sm text-gray-400">
          <i class="pi pi-bell text-2xl opacity-60" />
          <span>{{ t('notification.empty') }}</span>
        </div>

      <div v-else class="flex flex-col gap-2">
        <div
          v-for="(item, index) in list"
          :key="index"
          class="flex items-start gap-2.5 rounded-lg border p-3"
          :class="item.source === 'heartbeat'
            ? 'border-sky-200 bg-sky-50/60 dark:border-sky-800/60 dark:bg-sky-900/20'
            : 'border-amber-200 bg-amber-50/60 dark:border-amber-800/60 dark:bg-amber-900/20'">
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

      <div v-if="list.length > 0" class="flex justify-end border-t border-gray-200 dark:border-gray-700 pt-2">
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
  set: (v) => {
    emits('update:modelValue', v);
    if (v) clearUnread();
  },
});

// 父组件通过 v-model 打开 dialog 时，computed 的 setter 不会触发；
// 此处监听 modelValue，确保父组件打开弹窗时也能清除未读徽标。
watch(
  () => props.modelValue,
  (open) => {
    if (open) clearUnread();
  },
);

/** 单条通知（合并后的条目） */
interface NotificationItem {
  source: 'heartbeat' | 'cron';
  content: string;
  count: number;
  time: string; // 首次出现时间
}

/** 全部通知（含未读计数） */
const items = ref<NotificationItem[]>([]);

/** 列表按新→旧展示 */
const list = computed<NotificationItem[]>(() => [...items.value].reverse());

/** 当前是否已读（dialog 打开过则视为已读） */
const read = ref(false);

/** 徽标未读数：已读时为 0 */
const unreadCount = computed<number>(() => (read.value ? 0 : items.value.reduce((sum, i) => sum + i.count, 0)));

/** 打开 dialog 时清除未读状态（内容保留，仅徽标清零） */
const clearUnread = () => {
  read.value = true;
  emits('changed', 0);
};

/** 清空全部通知 */
const clearAll = () => {
  items.value = [];
  emits('changed', 0);
};

/** 通知内容（WS 推送的 content 可能为对象或字符串） */
const contentOf = (payload: unknown): string => {
  if (payload == null) return '';
  if (typeof payload === 'string') return payload.trim();
  try {
    const s = JSON.stringify(payload);
    return (s && s !== '{}') ? s : '';
  } catch {
    return '';
  }
};

/** 判断通知来源（内容中是否包含任务关键词） */
const sourceOf = (content: string): NotificationItem['source'] => {
  // 服务器推送时 content 前缀携带来源标记；无标记时按内容关键词兜底
  if (/^(heartbeat|cron):\s*/.test(content)) return content.startsWith('heartbeat:') ? 'heartbeat' : 'cron';
  return /\b(cron|定时|定時)\b/i.test(content) ? 'cron' : 'heartbeat';
};

/** 处理一条通知：与「上一条（最新一条）」内容相同则合并计数 */
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
      time: dayjs().format('HH:mm:ss'),
    });
  }

  // 新通知到达时重置为未读（若 dialog 未打开）
  if (!visible.value) {
    read.value = false;
  }
  emits('changed', unreadCount.value);
};

// 建立 WS 通知订阅
useWs();
on('ws:notification', handleNotification);

// 通知父组件初始未读数
emits('changed', 0);
</script>
