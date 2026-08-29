<template>
  <Dialog
    v-model:visible="visible"
    :header="t('config.cron.title')"
    :modal="true"
    :closable="true"
    class="w-[95vw] md:w-[820px]"
    @show="loadJobs">
    <div class="flex flex-col gap-3">
      <div
        v-if="loading"
        class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 2rem; height: 2rem" />
      </div>
      <template v-else>
        <!-- Empty state -->
        <div
          v-if="!jobs.length"
          class="text-sm text-gray-400 dark:text-gray-500 pb-1">
          {{ t('config.cron.empty') }}
        </div>

        <!-- Add / New job (always visible, even when list is empty) -->
        <div class="flex justify-end">
          <Button
            :label="t('config.cron.addJob')"
            icon="pi pi-plus"
            severity="secondary"
            outlined
            size="small"
            @click="openNewJob" />
        </div>

        <!-- Job list -->
        <div class="flex flex-col gap-2">
          <div
            v-for="job in jobs"
            :key="job.id"
            class="flex flex-col gap-1 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2 min-w-0">
                <ToggleSwitch
                  :modelValue="job.enabled"
                  @change="toggleJob(job)" />
                <span class="font-semibold text-sm truncate">{{ job.name }}</span>
              </div>
              <div class="flex items-center gap-1 shrink-0">
                <Button
                  :label="t('config.cron.run')"
                  icon="pi pi-play"
                  size="small"
                  severity="success"
                  text
                  @click="runJob(job)" />
                <Button
                  :label="t('config.cron.edit')"
                  icon="pi pi-pencil"
                  size="small"
                  severity="secondary"
                  text
                  @click="openEditJob(job)" />
                <Button
                  :label="t('config.cron.delete')"
                  icon="pi pi-trash"
                  size="small"
                  severity="danger"
                  text
                  @click="removeJob(job)" />
              </div>
            </div>
            <div class="flex flex-col gap-0.5 text-xs text-gray-500 dark:text-gray-400">
              <div class="font-mono">{{ describeSchedule(job) }}</div>
              <div class="truncate">{{ job.payload.message }}</div>
              <div
                v-if="job.state?.nextRunAtMs"
                class="text-gray-400 dark:text-gray-500">
                {{ t('config.cron.nextRun') }}: {{ formatTime(job.state.nextRunAtMs) }}
              </div>
              <div
                v-if="job.state?.lastStatus"
                class="text-gray-400 dark:text-gray-500">
                {{ t('config.cron.lastStatus') }}: {{ job.state.lastStatus }}
                <span
                  v-if="job.state.lastError"
                  class="ml-1 text-red-500 dark:text-red-400">{{ job.state.lastError }}</span>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </Dialog>

  <!-- Add/Edit job dialog -->
  <Dialog
    v-model:visible="editing"
    :header="editingId ? t('config.cron.editTitle') : t('config.cron.addTitle')"
    :modal="true"
    :closable="true"
    class="w-[95vw] md:w-[640px]">
    <div class="flex flex-col gap-3">
      <div class="flex flex-col gap-1">
        <label class="text-sm">{{ t('config.cron.name') }}</label>
        <InputText
          v-model="form.name"
          class="w-full"
          :placeholder="t('config.cron.namePlaceholder')" />
      </div>

      <div class="flex flex-col gap-1">
        <label class="text-sm">{{ t('config.cron.scheduleType') }}</label>
        <div class="flex gap-2">
          <SelectButton
            v-model="form.scheduleType"
            :options="scheduleTypeOptions"
            optionLabel="label"
            optionValue="value"
            class="w-full" />
        </div>
      </div>

      <!-- Schedule-specific fields -->
      <div v-if="form.scheduleType === 'at'" class="flex flex-col gap-1">
        <label class="text-sm">{{ t('config.cron.atTime') }}</label>
        <Calendar
          v-model="form.atDate"
          showTime
          hourFormat="24"
          fluid
          class="w-full" />
      </div>

      <div v-else-if="form.scheduleType === 'every'" class="flex flex-col gap-1">
        <label class="text-sm">{{ t('config.cron.everyInterval') }}</label>
        <div class="flex items-center gap-2">
          <InputNumber
            v-model="form.everyValue"
            :min="1"
            class="w-32"
            :placeholder="t('config.cron.intervalValue')" />
          <Select
            v-model="form.everyUnit"
            :options="everyUnitOptions"
            optionLabel="label"
            optionValue="value"
            class="w-40" />
        </div>
      </div>

      <div v-else class="flex flex-col gap-1">
        <label class="text-sm">{{ t('config.cron.cronExpr') }}</label>
        <InputText
          v-model="form.expr"
          class="w-full font-mono"
          placeholder="*/5 * * * *" />
      </div>

      <div class="flex flex-col gap-1">
        <label class="text-sm">{{ t('config.cron.message') }}</label>
        <Textarea
          v-model="form.message"
          class="w-full font-mono text-sm"
          rows="3"
          autoResize
          :placeholder="t('config.cron.messagePlaceholder')" />
      </div>

      <div class="flex items-center gap-2">
        <Checkbox
          v-model="form.deliver"
          :binary="true"
          inputId="cron-deliver" />
        <label for="cron-deliver" class="text-sm">{{ t('config.cron.deliver') }}</label>
      </div>

      <template v-if="form.deliver">
        <div class="flex flex-col gap-1">
          <label class="text-sm">{{ t('config.cron.channel') }}</label>
          <InputText
            v-model="form.channel"
            class="w-full"
            :placeholder="t('config.cron.channelPlaceholder')" />
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-sm">{{ t('config.cron.to') }}</label>
          <InputText
            v-model="form.to"
            class="w-full"
            :placeholder="t('config.cron.toPlaceholder')" />
        </div>
      </template>

      <div class="flex items-center gap-2">
        <Checkbox
          v-model="form.deleteAfterRun"
          :binary="true"
          inputId="cron-delete-after-run" />
        <label for="cron-delete-after-run" class="text-sm">{{ t('config.cron.deleteAfterRun') }}</label>
      </div>
    </div>

    <template #footer>
      <div class="flex gap-2 justify-end">
        <Button
          :label="t('config.cancel')"
          icon="pi pi-times"
          severity="secondary"
          @click="cancelEdit" />
        <Button
          :label="t('config.save')"
          icon="pi pi-check"
          :loading="saving"
          :disabled="!canSaveEdit"
          @click="handleSaveJob" />
      </div>
    </template>
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed } from 'vue';
import { useI18n } from 'vue-i18n';
import {
  listCronJobs,
  addCronJob,
  updateCronJob,
  runCronJob,
  enableCronJob,
  deleteCronJob,
  type CronJob,
  type CronSchedule,
} from '@/composables/bridge';

  const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

const loading = ref(false);
const saving = ref(false);
const jobs = ref<CronJob[]>([]);

// ── Add / Edit form state ─────────────────────────
const editing = ref(false);
const editingId = ref<string | null>(null);

const scheduleTypeOptions = computed(() => [
  { label: t('config.cron.typeAt'), value: 'at' },
  { label: t('config.cron.typeEvery'), value: 'every' },
  { label: t('config.cron.typeCron'), value: 'cron' }
]);

const everyUnitOptions = [
  { label: '秒 / seconds', value: 's' },
  { label: '分 / minutes', value: 'm' },
  { label: '时 / hours', value: 'h' },
  { label: '天 / days', value: 'd' }
];

const DAY_MS = 24 * 60 * 60 * 1000;
const HOUR_MS = 60 * 60 * 1000;
const MINUTE_MS = 60 * 1000;
const SECOND_MS = 1000;

const form = ref({
  name: '',
  scheduleType: 'every' as 'at' | 'every' | 'cron',
  atDate: new Date() as Date | null,
  everyValue: 5 as number | null,
  everyUnit: 'm' as string,
  expr: '' as string,
  message: '',
  deliver: false,
  channel: '' as string | null,
  to: '' as string | null,
  deleteAfterRun: false
});

const canSaveEdit = computed(() => {
  if (saving.value) return false;
  if (!form.value.name.trim()) return false;
  if (!form.value.message.trim()) return false;
  if (form.value.scheduleType === 'at' && !form.value.atDate) return false;
  if (form.value.scheduleType === 'every' && (!form.value.everyValue || form.value.everyValue <= 0)) return false;
  if (form.value.scheduleType === 'cron' && !form.value.expr.trim()) return false;
  return true;
});

function everyToMs(): number | null {
  const v = form.value.everyValue;
  if (!v || v <= 0) return null;
  switch (form.value.everyUnit) {
    case 's': return v * SECOND_MS;
    case 'm': return v * MINUTE_MS;
    case 'h': return v * HOUR_MS;
    case 'd': return v * DAY_MS;
    default: return null;
  }
}

function buildSchedule(): CronSchedule {
  switch (form.value.scheduleType) {
    case 'at':
      return { kind: 'at', atMs: form.value.atDate ? form.value.atDate.getTime() : null };
    case 'cron':
      return { kind: 'cron', expr: form.value.expr.trim() };
    case 'every':
    default:
      return { kind: 'every', everyMs: everyToMs() };
  }
}

function resetForm() {
  form.value = {
    name: '',
    scheduleType: 'every',
    atDate: new Date(),
    everyValue: 5,
    everyUnit: 'm',
    expr: '',
    message: '',
    deliver: false,
    channel: null,
    to: null,
    deleteAfterRun: false
  };
}

function openNewJob() {
  resetForm();
  editingId.value = null;
  editing.value = true;
}

function openEditJob(job: CronJob) {
  editingId.value = job.id;
  const s = job.schedule;
  // Reconstruct schedule-type specific fields from the stored schedule.
  let scheduleType: 'at' | 'every' | 'cron' = 'cron';
  if (s.kind === 'at') scheduleType = 'at';
  else if (s.kind === 'every') scheduleType = 'every';

  let atDate: Date | null = null;
  if (s.atMs) atDate = new Date(s.atMs);

  let everyValue: number | null = 5;
  let everyUnit = 'm';
  if (s.everyMs) {
    // Choose the largest whole unit that evenly divides the interval.
    if (s.everyMs % DAY_MS === 0) {
      everyValue = s.everyMs / DAY_MS;
      everyUnit = 'd';
    } else if (s.everyMs % HOUR_MS === 0) {
      everyValue = s.everyMs / HOUR_MS;
      everyUnit = 'h';
    } else if (s.everyMs % MINUTE_MS === 0) {
      everyValue = s.everyMs / MINUTE_MS;
      everyUnit = 'm';
    } else {
      everyValue = s.everyMs / SECOND_MS;
      everyUnit = 's';
    }
  }

  form.value = {
    name: job.name,
    scheduleType,
    atDate,
    everyValue,
    everyUnit,
    expr: s.expr ?? '',
    message: job.payload.message,
    deliver: job.payload.deliver,
    channel: job.payload.channel ?? null,
    to: job.payload.to ?? null,
    deleteAfterRun: job.deleteAfterRun
  };
  editing.value = true;
}

function cancelEdit() {
  editing.value = false;
  editingId.value = null;
}

async function handleSaveJob() {
  if (!canSaveEdit.value) return;
  saving.value = true;
  try {
    const payload = {
      name: form.value.name.trim(),
      message: form.value.message.trim(),
      schedule: buildSchedule(),
      deliver: form.value.deliver,
      channel: form.value.deliver ? form.value.channel : null,
      to: form.value.deliver ? form.value.to : null,
      delete_after_run: form.value.deleteAfterRun
    };
    if (editingId.value) {
      await updateCronJob(editingId.value, payload);
    } else {
      await addCronJob(payload);
    }
    editing.value = false;
    editingId.value = null;
    await loadJobs();
  } catch (e) {
    console.error('[CronDialog] Failed to save job:', e);
  } finally {
    saving.value = false;
  }
}

// ── Job list actions ──────────────────────────────
async function toggleJob(job: CronJob) {
  try {
    await enableCronJob(job.id, !job.enabled);
    await loadJobs();
  } catch (e) {
    console.error('[CronDialog] Failed to toggle job:', e);
  }
}

async function runJob(job: CronJob) {
  try {
    await runCronJob(job.id, true);
    await loadJobs();
  } catch (e) {
    console.error('[CronDialog] Failed to run job:', e);
  }
}

async function removeJob(job: CronJob) {
  try {
    await deleteCronJob(job.id);
    await loadJobs();
  } catch (e) {
    console.error('[CronDialog] Failed to delete job:', e);
  }
}

// ── Display helpers ───────────────────────────────
function describeSchedule(job: CronJob): string {
  const s = job.schedule;
  switch (s.kind) {
    case 'at':
      return s.atMs ? t('config.cron.descAt', { time: formatTime(s.atMs) }) : t('config.cron.descAtEmpty');
    case 'every':
      return fmtInterval(s.everyMs);
    case 'cron':
    default:
      return s.expr ?? '';
  }
}

function fmtInterval(ms?: number | null): string {
  if (!ms) return '';
  if (ms % DAY_MS === 0) return t('config.cron.everyDays', { n: ms / DAY_MS });
  if (ms % HOUR_MS === 0) return t('config.cron.everyHours', { n: ms / HOUR_MS });
  if (ms % MINUTE_MS === 0) return t('config.cron.everyMinutes', { n: ms / MINUTE_MS });
  return t('config.cron.everySeconds', { n: ms / SECOND_MS });
}

function formatTime(ms: number): string {
  return new Date(ms).toLocaleString();
}

async function loadJobs() {
  loading.value = true;
  try {
    const data = await listCronJobs(true);
    jobs.value = data.jobs ?? [];
  } catch (e) {
    console.error('[CronDialog] Failed to load jobs:', e);
    jobs.value = [];
  } finally {
    loading.value = false;
  }
}
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "cron": {
        "title": "定时任务",
        "addJob": "新建任务",
        "empty": "暂无定时任务。点击「新建任务」添加。",
        "run": "运行",
        "edit": "编辑",
        "delete": "删除",
        "addTitle": "新建定时任务",
        "editTitle": "编辑定时任务",
        "name": "任务名称",
        "namePlaceholder": "例如：每日早安问候",
        "scheduleType": "调度方式",
        "typeAt": "指定时刻",
        "typeEvery": "每隔多久",
        "typeCron": "Cron 表达式",
        "atTime": "执行时刻",
        "everyInterval": "执行间隔",
        "intervalValue": "数值",
        "cronExpr": "Cron 表达式",
        "message": "任务消息",
        "messagePlaceholder": "请输入要执行的任务内容",
        "deliver": "推送到渠道",
        "channel": "渠道名称",
        "channelPlaceholder": "例如：default",
        "to": "接收人/群",
        "toPlaceholder": "可选",
        "deleteAfterRun": "运行后自动删除",
        "nextRun": "下次运行",
        "lastStatus": "上次状态",
        "descAt": "在 {time} 执行",
        "descAtEmpty": "未设定时刻",
        "everySeconds": "每 {n} 秒",
        "everyMinutes": "每 {n} 分钟",
        "everyHours": "每 {n} 小时",
        "everyDays": "每 {n} 天"
      }
    }
  },
  "en": {
    "config": {
      "cron": {
        "title": "Cron Tasks",
        "addJob": "New task",
        "empty": "No cron tasks yet. Click \"New task\" to add one.",
        "run": "Run",
        "edit": "Edit",
        "delete": "Delete",
        "addTitle": "New Cron Task",
        "editTitle": "Edit Cron Task",
        "name": "Task name",
        "namePlaceholder": "e.g. Daily good-morning greeting",
        "scheduleType": "Schedule type",
        "typeAt": "Specific time",
        "typeEvery": "Repeat every",
        "typeCron": "Cron expression",
        "atTime": "Execution time",
        "everyInterval": "Interval",
        "intervalValue": "Value",
        "cronExpr": "Cron expression",
        "message": "Task message",
        "messagePlaceholder": "Enter the task content to execute",
        "deliver": "Push to channel",
        "channel": "Channel",
        "channelPlaceholder": "e.g. default",
        "to": "Recipient / group",
        "toPlaceholder": "Optional",
        "deleteAfterRun": "Delete after run",
        "nextRun": "Next run",
        "lastStatus": "Last status",
        "descAt": "Run at {time}",
        "descAtEmpty": "No time set",
        "everySeconds": "Every {n} seconds",
        "everyMinutes": "Every {n} minutes",
        "everyHours": "Every {n} hours",
        "everyDays": "Every {n} days"
      }
    }
  },
  "ja": {
    "config": {
      "cron": {
        "title": "クーロンタスク",
        "addJob": "新規タスク",
        "empty": "クーロンタスクはまだありません。「新規タスク」をクリックして追加してください。",
        "run": "実行",
        "edit": "編集",
        "delete": "削除",
        "addTitle": "新しいクーロンタスク",
        "editTitle": "クーロンタスクを編集",
        "name": "タスク名",
        "namePlaceholder": "例：毎朝の挨拶",
        "scheduleType": "スケジュール方式",
        "typeAt": "指定時刻",
        "typeEvery": "間隔で繰り返し",
        "typeCron": "Cron 式",
        "atTime": "実行時刻",
        "everyInterval": "実行間隔",
        "intervalValue": "値",
        "cronExpr": "Cron 式",
        "message": "タスク内容",
        "messagePlaceholder": "実行するタスクの内容を入力",
        "deliver": "チャネルへ配信",
        "channel": "チャネル名",
        "channelPlaceholder": "例：default",
        "to": "受信者 / グループ",
        "toPlaceholder": "任意",
        "deleteAfterRun": "実行後に自動削除",
        "nextRun": "次回実行",
        "lastStatus": "前回の状態",
        "descAt": "{time} に実行",
        "descAtEmpty": "時刻が未設定",
        "everySeconds": "{n} 秒ごと",
        "everyMinutes": "{n} 分ごと",
        "everyHours": "{n} 時間ごと",
        "everyDays": "{n} 日ごと"
      }
    }
  },
  "ko": {
    "config": {
      "cron": {
        "title": "크론 작업",
        "addJob": "새 작업",
        "empty": "크론 작업이 없습니다. \"새 작업\"을 클릭하여 추가하세요.",
        "run": "실행",
        "edit": "편집",
        "delete": "삭제",
        "addTitle": "새 크론 작업",
        "editTitle": "크론 작업 편집",
        "name": "작업 이름",
        "namePlaceholder": "예: 매일 아침 인사",
        "scheduleType": "스케줄 방식",
        "typeAt": "지정 시각",
        "typeEvery": "간격 반복",
        "typeCron": "Cron 표현식",
        "atTime": "실행 시각",
        "everyInterval": "실행 간격",
        "intervalValue": "값",
        "cronExpr": "Cron 표현식",
        "message": "작업 내용",
        "messagePlaceholder": "실행할 작업 내용을 입력하세요",
        "deliver": "채널로 전송",
        "channel": "채널 이름",
        "channelPlaceholder": "예: default",
        "to": "수신자 / 그룹",
        "toPlaceholder": "선택 사항",
        "deleteAfterRun": "실행 후 자동 삭제",
        "nextRun": "다음 실행",
        "lastStatus": "마지막 상태",
        "descAt": "{time}에 실행",
        "descAtEmpty": "시각 미설정",
        "everySeconds": "매 {n}초",
        "everyMinutes": "매 {n}분",
        "everyHours": "매 {n}시간",
        "everyDays": "매 {n}일"
      }
    }
  }
}
</i18n>
