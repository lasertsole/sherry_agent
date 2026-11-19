<template>
  <div
    class="meeting-list-panel flex flex-col h-full"
    data-test="meeting-list-panel">
    <div class="flex items-center justify-between px-3 py-2">
      <span class="font-medium">{{ t('nav.meetings') }}</span>
      <Button
        icon="pi pi-plus"
        size="small"
        data-test="meeting-create-open"
        :aria-label="t('meetings.create')"
        @click="showCreate = true" />
    </div>
    <div class="flex-1 overflow-y-auto px-3">
      <div
        v-if="store.meetings.length === 0"
        class="text-center py-4 text-gray-400"
        data-test="meeting-empty">
        {{ t('meetings.empty') }}
      </div>
      <div
        v-else
        class="space-y-2">
        <ListItemCard
          v-for="meeting in store.meetings"
          :key="meeting.id"
          :icon="'pi pi-video'"
          :title="meeting.topic"
          :badges="[{ value: t(`meetings.status.${meeting.status}`), severity: statusSeverity(meeting.status) }]"
          :meta="[
            { value: `${meeting.participant_count} ${t('meetings.peopleSuffix')}` },
            { value: `${meeting.iteration_count}/40` }
          ]"
          data-test="meeting-card"
          @click="enterMeeting(meeting.id)" />
      </div>
    </div>
    <MeetingCreate
      v-if="showCreate"
      v-model:visible="showCreate"
      @created="onCreated" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { useMeetingStore, type MeetingStatus } from '@/stores/meeting';
import MeetingCreate from '../MeetingCreate.vue';
import ListItemCard from '@/components/ListItemCard.vue';

const { t } = useI18n();
const store = useMeetingStore();
const router = useRouter();
const showCreate = ref(false);

function statusSeverity(status: MeetingStatus): 'info' | 'warn' | 'success' | 'secondary' {
  switch (status) {
    case 'in_progress':
      return 'info';
    case 'waiting_user':
      return 'warn';
    case 'ended':
      return 'success';
    default:
      return 'secondary';
  }
}

function enterMeeting(id: string): void {
  void router.push(`/meeting/${id}`);
}
function onCreated(id: string): void {
  showCreate.value = false;
  enterMeeting(id);
}

onMounted(() => void store.loadMeetings());
</script>
