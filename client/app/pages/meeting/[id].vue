<template>
  <div
    class="meeting-page flex flex-col flex-1 min-h-0"
    data-test="meeting-page">
    <!-- TitleBar comes from the default layout (S4 patch). -->
    <header class="flex items-center gap-3 px-4 py-2 border-b border-solid border-gray-light dark:border-gray-dark">
      <Button
        icon="pi pi-arrow-left"
        text
        :aria-label="t('meetings.back')"
        data-test="meeting-back"
        @click="goBack" />
      <div class="min-w-0">
        <h1 class="font-medium truncate">{{ store.currentMeeting?.topic ?? id }}</h1>
        <p class="text-xs text-gray-400">
          {{ t(`meetings.status.${store.currentMeeting?.status ?? 'in_progress'}`) }} · {{ t('meetings.iteration') }}
          {{ store.currentMeeting?.iteration_count ?? 0 }}/40
        </p>
      </div>
      <div class="ml-auto flex gap-2">
        <Button
          :label="t('meetings.confirmEnd')"
          size="small"
          severity="danger"
          data-test="meeting-confirm-end"
          @click="store.confirmEnd()" />
        <Button
          :label="t('meetings.execute')"
          size="small"
          data-test="meeting-execute"
          @click="onExecute" />
      </div>
    </header>

    <div class="flex flex-1 min-h-0">
      <!-- Participants + execution plan -->
      <aside class="w-56 shrink-0 border-r border-solid border-gray-light dark:border-gray-dark p-3 overflow-y-auto">
        <h2 class="text-sm font-medium mb-2">{{ t('meetings.participants') }}</h2>
        <ul
          class="space-y-1 text-sm"
          data-test="meeting-participants">
          <li
            v-for="phase in store.executionPlan?.phases ?? []"
            :key="phase.id"
            class="flex items-center gap-2 text-gray-500">
            <i :class="phase.prerequisites.length ? 'pi pi-check-circle' : 'pi pi-clock'" />
            <span class="truncate">{{ phase.id }}</span>
          </li>
        </ul>
        <p
          v-if="store.currentSpeakerId"
          class="mt-3 text-xs text-theme-main"
          data-test="meeting-current-speaker">
          {{ t('meetings.speaking') }}: {{ store.currentSpeakerId }}
        </p>
      </aside>

      <!-- Message stream (MessageBubble reuse) -->
      <section class="flex-1 min-w-0 flex flex-col">
        <div
          ref="streamRef"
          class="flex-1 overflow-y-auto p-4 space-y-4"
          data-test="meeting-stream">
          <MessageBubble
            v-for="msg in store.orderedMessages"
            :key="msg.sequence"
            :content="msg.content"
            :speaker-name="msg.speaker_name"
            :speaker-kind="msg.speaker_kind"
            :role-badge="msg.message_role === 'summary' ? t('meetings.roleSummary') : undefined"
            :thinking="msg.thinking_md"
            data-test="meeting-message" />
          <div
            v-if="store.messages.length === 0"
            class="text-center text-gray-400 py-8"
            data-test="meeting-empty-stream">
            {{ t('meetings.emptyStream') }}
          </div>
        </div>
        <div class="border-t border-solid border-gray-light dark:border-gray-dark p-3 flex gap-2">
          <InputText
            v-model="interruptText"
            class="flex-1"
            :placeholder="t('meetings.interruptPlaceholder')"
            data-test="meeting-interrupt-input"
            @keyup.enter="sendInterrupt" />
          <Button
            :label="t('meetings.interrupt')"
            :disabled="!interruptText.trim()"
            data-test="meeting-interrupt-send"
            @click="sendInterrupt" />
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
import { useMeetingStore } from '@/stores/meeting';
import MessageBubble from '@/components/chat/MessageBubble.vue';

definePageMeta({ layout: 'default' });

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const store = useMeetingStore();
const id = String(route.params.id ?? '');
const interruptText = ref('');
const streamRef = ref<HTMLElement | null>(null);

function goBack(): void {
  void router.push('/home');
}

async function sendInterrupt(): Promise<void> {
  if (!interruptText.value.trim()) return;
  const content = interruptText.value;
  interruptText.value = '';
  await store.interrupt(content);
  await nextTick();
  scrollToBottom();
}

function onExecute(): void {
  void store.execute();
  goBack();
}

function scrollToBottom(): void {
  const el = streamRef.value;
  if (el) el.scrollTop = el.scrollHeight;
}

watch(
  () => store.messages.length,
  () => void nextTick().then(scrollToBottom)
);

onMounted(() => {
  void store.openMeeting(id);
  // The meeting WS endpoint is not shipped; handleFrame is ready for it, and
  // the mock store seeds the stream so the page is fully navigable today.
});
onBeforeUnmount(() => store.closeMeeting());
</script>
