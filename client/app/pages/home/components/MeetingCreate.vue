<template>
  <Dialog
    :visible="visible"
    :modal="true"
    :style="{ width: '480px' }"
    :header="t('meetings.create')"
    data-test="meeting-create-dialog"
    @update:visible="$emit('update:visible', $event)">
    <div class="flex flex-col gap-3">
      <FormField :label="t('meetings.topic')">
        <InputText
          v-model="topic"
          class="w-full"
          data-test="meeting-topic" />
      </FormField>
      <FormField
        :label="t('meetings.participants')"
        :hint="t('meetings.participantsHint')">
        <div class="flex flex-col gap-1 max-h-48 overflow-y-auto">
          <label
            v-for="expert in expertStore.experts"
            :key="expert.name"
            class="flex items-center gap-2 text-sm">
            <Checkbox
              v-model="selected"
              :input-id="`mp-${expert.name}`"
              :value="expert.name" />
            <span>{{ expert.name }}</span>
          </label>
          <span
            v-if="expertStore.experts.length === 0"
            class="text-xs text-gray-400"
            >{{ t('meetings.noExperts') }}</span
          >
        </div>
      </FormField>
    </div>
    <template #footer>
      <Button
        :label="t('common.cancel')"
        text
        data-test="meeting-create-cancel"
        @click="$emit('update:visible', false)" />
      <Button
        :label="t('meetings.create')"
        icon="pi pi-check"
        :disabled="!topic.trim() || selected.length < 2 || creating"
        :loading="creating"
        data-test="meeting-create-confirm"
        @click="confirm" />
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useMeetingStore } from '@/stores/meeting';
import { useExpertRegistryStore } from '@/stores/expert-registry';
import FormField from '@/components/FormField.vue';

const visible = defineModel<boolean>('visible', { default: false });
const emit = defineEmits<{ created: [id: string] }>();
const { t } = useI18n();
const store = useMeetingStore();
const expertStore = useExpertRegistryStore();
const topic = ref('');
const selected = ref<string[]>([]);
const creating = ref(false);

watch(visible, open => {
  if (open) {
    topic.value = '';
    selected.value = [];
    void expertStore.loadAll();
  }
});

async function confirm(): Promise<void> {
  if (!topic.value.trim() || selected.value.length < 2) return;
  creating.value = true;
  try {
    const meeting = await store.createMeeting(topic.value, [...selected.value]);
    emit('created', meeting.id);
  } finally {
    creating.value = false;
  }
}
</script>
