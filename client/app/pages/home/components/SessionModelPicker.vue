<template>
  <div class="flex items-center gap-2 pl-2">
    <!-- Collapsed picker: the trigger names the current model; the option list
         (跟随环境配置 + the env-config MAIN_LLM profiles) opens on click. The
         control's own name is the list header, so the toolbar row stays narrow.
         Switching is allowed at any moment — a mid-turn choice is parked
         (clock icon) and lands on the next turn. -->
    <Button
      variant="text"
      size="small"
      class="model-trigger"
      :label="displayLabel"
      icon="pi pi-angle-down"
      icon-pos="right"
      :title="triggerTitle"
      :aria-label="triggerAriaLabel"
      @click="toggleMenu" />
    <i
      v-if="pending"
      class="pi pi-clock text-xs text-theme-main"
      :title="t('sessionModelPicker.pending')"
      :aria-label="t('sessionModelPicker.pending')"></i>
    <Menu
      ref="modelMenu"
      :model="menuItems"
      popup>
      <!-- List header: names the control at the top of the option list. -->
      <template #start>
        <div class="px-3 pt-1 pb-2 text-xs font-medium text-gray-500 dark:text-gray-400 select-none">
          {{ t('sessionModelPicker.label') }}
        </div>
      </template>
    </Menu>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import type { LlmProfile } from '~/stores/llm-profiles';
import type { SessionModelProfile } from '~/composables/bridge/session';
import { ENV_MODEL_ID } from '~/stores/session-model';

const props = defineProps<{ sessionId: string }>();

const { t } = useI18n();
/** Per-session main-model control store (hydrated from the backend). */
const store = useSessionModelStore();
/** Thinking control: its MODE follows the model, so re-hydrate after a switch. */
const thinking = useThinkingStore();
/** The env-config profiles: the SAME option list the 环境配置 panel shows. */
const profiles = useLlmProfilesStore();

/** Env-config group whose profiles the picker mirrors. */
const MAIN_LLM_GROUP = 'MAIN_LLM';

/** Popup list of the available models, opened by the collapsed trigger. */
const modelMenu = ref();

/**
 * First non-empty param value whose key carries one of the suffixes.
 * Suffix order wins, so `_API_NAME` resolves before the shorter `_NAME`.
 * @param params Profile parameters (env keys → values).
 * @param suffixes Key suffixes to try, in priority order.
 */
const paramBySuffix = (params: Record<string, string>, suffixes: string[]): string | undefined => {
  for (const suffix of suffixes) {
    for (const [key, value] of Object.entries(params)) {
      if (key.endsWith(suffix) && value) return value;
    }
  }
  return undefined;
};

/**
 * Map an env-config profile onto the backend's descriptor shape.
 * @param profile
 * @returns The descriptor, or null when the profile has no model name (an
 *          incomplete entry is not selectable).
 */
const toDescriptor = (profile: LlmProfile): SessionModelProfile | null => {
  const model = paramBySuffix(profile.params, ['_API_NAME', '_NAME']);
  if (!model) return null;
  return {
    id: profile.id,
    label: profile.label,
    model,
    provider: paramBySuffix(profile.params, ['_PROVIDER']),
    base_url: paramBySuffix(profile.params, ['_API_BASE', '_BASE_URL']),
    api_key: paramBySuffix(profile.params, ['_API_KEY'])
  };
};

/** Selectable options: the env-config MAIN_LLM profiles with a model name. */
const options = computed(() =>
  profiles.listFor(MAIN_LLM_GROUP).flatMap(profile => {
    const descriptor = toDescriptor(profile);
    return descriptor ? [{ profile, descriptor }] : [];
  })
);

/** The session's current selection (profile id, or `env`). */
const selectedId = computed(() => store.currentId(props.sessionId));

/**
 * Whether the current choice is parked: made while a turn was running, so it
 * lands on the next turn (the running turn keeps its model).
 */
const pending = computed(() => store.isPending(props.sessionId));

/** Label of the `env` entry: the env-configured model, else a generic name. */
const envLabel = computed(() => store.envModel.model || t('sessionModelPicker.followEnv'));

/** Model name currently in effect, whatever the source (env or a profile). */
const currentModel = computed(() => {
  if (selectedId.value === ENV_MODEL_ID) return envLabel.value;
  const override = store.overrideBySession[props.sessionId];
  if (override?.model) return override.model;
  return (
    options.value.find(option => option.descriptor.id === selectedId.value)?.descriptor.model ??
    t('sessionModelPicker.followEnv')
  );
});

/**
 * Trigger text: the model name only — the toolbar row is tight, so the
 * "跟随环境配置" wording lives in the menu entry instead. Long names are cut
 * short; the full name stays in the title/aria text.
 */
const displayLabel = computed(() =>
  currentModel.value.length > 16 ? `${currentModel.value.slice(0, 15)}…` : currentModel.value
);

/** Full text for the tooltip / screen readers (untruncated). */
const triggerTitle = computed(() =>
  pending.value ? `${currentModel.value} · ${t('sessionModelPicker.pending')}` : currentModel.value
);

/**
 * Accessible name of the trigger: the control's purpose plus the current value,
 * since the visible name lives in the list header rather than in the row.
 */
const triggerAriaLabel = computed(() => `${t('sessionModelPicker.a11y')}：${triggerTitle.value}`);

/** Menu items: 跟随环境配置 first, then one entry per selectable profile. */
const menuItems = computed(() => [
  {
    label: t('sessionModelPicker.followEnvWith', { model: envLabel.value }),
    icon: selectedId.value === ENV_MODEL_ID ? 'pi pi-check' : undefined,
    command: () => handleSelect(null)
  },
  ...options.value.map(({ descriptor }) => ({
    label: descriptor.model,
    icon: selectedId.value === descriptor.id ? 'pi pi-check' : undefined,
    command: () => handleSelect(descriptor)
  }))
]);

/** Hydrate on first mount and whenever the session changes. */
watch(
  () => props.sessionId,
  sessionId => {
    if (sessionId) store.hydrate(sessionId);
  },
  { immediate: true }
);

/**
 * Switch the session's model (null = follow the env config). Allowed at any
 * moment: a mid-turn choice is parked by the backend and applies next turn.
 * The thinking control's MODE (switch vs 低/高/最高 selector) is a property of
 * the chosen model, so it is re-hydrated once the write settles.
 * @param descriptor
 */
const handleSelect = async (descriptor: SessionModelProfile | null) => {
  await store.select(props.sessionId, descriptor);
  await thinking.hydrate(props.sessionId);
};

/**
 * Open the options popup.
 * @param event
 */
const toggleMenu = (event: Event) => {
  modelMenu.value?.toggle(event);
};
</script>

<style scoped>
/* Compact trigger: the model name is the widest text in the toolbar row. */
.model-trigger :deep(.p-button-label) {
  font-size: 0.75rem;
  max-width: 7.5rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
