<template>
  <div
    class="expert-edit-panel p-4 overflow-y-auto h-full"
    data-test="expert-edit-panel">
    <h3 class="font-medium mb-4">{{ isNew ? t('experts.create') : t('experts.edit') }}：{{ form.name || '...' }}</h3>

    <FormField
      :label="t('experts.name')"
      :hint="t('experts.nameHint')">
      <InputText
        v-model="form.name"
        :disabled="!isNew"
        class="w-full"
        data-test="expert-form-name" />
    </FormField>
    <FormField :label="t('experts.description')">
      <InputText
        v-model="form.description"
        class="w-full"
        data-test="expert-form-description" />
    </FormField>
    <FormField
      :label="t('experts.personaPrompt')"
      :hint="t('experts.personaHint')">
      <Textarea
        v-model="form.persona_prompt"
        rows="6"
        class="w-full"
        data-test="expert-form-persona" />
    </FormField>
    <FormField :label="t('experts.model')">
      <Select
        v-model="form.model"
        :options="modelOptions"
        option-label="label"
        option-value="value"
        class="w-full"
        data-test="expert-form-model" />
    </FormField>
    <FormField :label="t('experts.thinking')">
      <Select
        v-model="form.thinking"
        :options="thinkingLevels"
        class="w-full"
        data-test="expert-form-thinking" />
    </FormField>
    <FormField :label="t('experts.role')">
      <Select
        v-model="form.functional_role"
        :options="roles"
        class="w-full"
        data-test="expert-form-role" />
    </FormField>
    <FormField
      :label="t('experts.skills')"
      :hint="t('experts.skillsHint')">
      <MultiSelect
        v-model="form.skills"
        :options="availableSkills"
        option-label="name"
        option-value="name"
        class="w-full"
        data-test="expert-form-skills" />
    </FormField>
    <FormField :label="t('experts.mcpServers')">
      <InputText
        v-model="mcpInput"
        :placeholder="t('experts.mcpHint')"
        class="w-full"
        data-test="expert-form-mcp"
        @keyup.enter="addMcp" />
      <div
        v-if="form.mcp_servers.length"
        class="flex flex-wrap gap-1 mt-2">
        <span
          v-for="server in form.mcp_servers"
          :key="server"
          class="px-2 py-1 rounded text-xs bg-gray-100 dark:bg-gray-700"
          data-test="expert-mcp-chip">
          {{ server }}
          <button
            type="button"
            :aria-label="t('common.delete')"
            @click="removeMcp(server)">
            ×
          </button>
        </span>
      </div>
    </FormField>
    <FormField :label="t('experts.timeout')">
      <InputNumber
        v-model="form.step_timeout_seconds"
        :min="60"
        :max="7200"
        :step="60"
        suffix="s"
        class="w-full"
        data-test="expert-form-timeout" />
    </FormField>

    <div class="flex gap-2 mt-6">
      <Button
        :label="t('common.save')"
        icon="pi pi-check"
        data-test="expert-save"
        :loading="saving"
        @click="onSave" />
      <Button
        v-if="!isNew"
        :label="t('common.delete')"
        icon="pi pi-trash"
        severity="danger"
        data-test="expert-delete"
        @click="onDelete" />
      <Button
        :label="t('common.cancel')"
        text
        data-test="expert-cancel"
        @click="onCancel" />
    </div>
    <p
      v-if="store.error"
      class="text-xs text-red-500 mt-2"
      data-test="expert-error">
      {{ store.error }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useExpertRegistryStore, type ExpertConfig } from '@/stores/expert-registry';
import type { RightSidebarTabPayload } from '@/stores/right-sidebar';
import FormField from '@/components/FormField.vue';

const props = defineProps<{ payload?: RightSidebarTabPayload }>();
const emit = defineEmits<{ saved: [] }>();
const { t } = useI18n();
const store = useExpertRegistryStore();

const expertName = computed(() => props.payload?.expertName as string | null | undefined);
const isNew = computed(() => !expertName.value);
const form = ref<ExpertConfig>(store.newConfig());
const mcpInput = ref('');
const saving = ref(false);

watch(
  expertName,
  name => {
    const found = name ? store.byName.get(name) : undefined;
    form.value = found
      ? { ...found, skills: [...found.skills], mcp_servers: [...found.mcp_servers] }
      : store.newConfig();
    mcpInput.value = '';
  },
  { immediate: true }
);

const modelOptions = [
  { label: 'claude-3-5-sonnet', value: 'claude-3-5-sonnet' },
  { label: 'gpt-4', value: 'gpt-4' },
  { label: 'deepseek', value: 'deepseek' }
];
const thinkingLevels = ['off', 'low', 'medium', 'high'];
const roles = ['executor', 'researcher', 'reviewer', 'librarian', 'general'];
const availableSkills = computed(() =>
  Array.from(new Set(store.experts.flatMap(expert => expert.skills))).map(name => ({ name }))
);

function addMcp(): void {
  const value = mcpInput.value.trim();
  if (value && !form.value.mcp_servers.includes(value)) form.value.mcp_servers.push(value);
  mcpInput.value = '';
}
function removeMcp(value: string): void {
  form.value.mcp_servers = form.value.mcp_servers.filter(item => item !== value);
}

async function onSave(): Promise<void> {
  if (!form.value.name.trim()) return;
  saving.value = true;
  try {
    if (isNew.value) await store.create({ ...form.value });
    else await store.update(expertName.value!, { ...form.value });
    emit('saved');
  } finally {
    saving.value = false;
  }
}

async function onDelete(): Promise<void> {
  if (isNew.value) return;
  saving.value = true;
  try {
    await store.remove(expertName.value!);
    emit('saved');
  } finally {
    saving.value = false;
  }
}

function onCancel(): void {
  emit('saved');
}
</script>
