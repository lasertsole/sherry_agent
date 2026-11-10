<template>
  <Teleport to="body">
    <div
      v-if="anchor"
      class="input-trigger-panel"
      data-test="input-trigger-panel"
      :style="panelStyle"
      @mousedown.prevent>
      <div class="trigger-head">
        <span>{{ kind === 'skill' ? t('chatInput.skillPickerTitle') : t('chatInput.commandPickerTitle') }}</span>
        <span class="trigger-query">{{ kind === 'skill' ? '$' : '/' }}{{ query }}</span>
      </div>

      <div
        ref="listRef"
        class="trigger-list">
        <button
          v-for="(row, index) in rows"
          :key="row.key"
          type="button"
          class="trigger-row"
          :class="{ active: index === activeIndex }"
          :data-index="index"
          @click="choose(row)"
          @mouseenter="activeIndex = index">
          <span class="trigger-name">{{ row.label }}</span>
          <span
            v-if="row.badge"
            class="trigger-badge"
            >{{ row.badge }}</span
          >
          <span
            v-if="row.description"
            class="trigger-desc"
            >{{ row.description }}</span
          >
        </button>

        <div
          v-if="!rows.length && !loading"
          class="trigger-empty">
          {{ kind === 'skill' ? t('chatInput.noSkills') : t('chatInput.noCommands') }}
        </div>
        <div
          v-if="loading"
          class="trigger-empty">
          {{ t('chatInput.loadingTriggers') }}
        </div>
      </div>

      <div
        v-if="argumentCommand"
        class="trigger-argument">
        <InputText
          ref="argumentInput"
          v-model="argumentValue"
          class="w-full"
          :placeholder="argumentCommand.argumentHint || t('chatInput.commandArguments')"
          data-test="trigger-argument-input"
          @keydown.enter.prevent="confirmArgument"
          @keydown.esc.prevent="cancelArgument" />
        <Button
          :label="t('chatInput.commandApply')"
          size="small"
          @click="confirmArgument" />
      </div>
    </div>
  </Teleport>
</template>

<script lang="ts" setup>
import { computed, nextTick, onMounted, ref, useTemplateRef, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { CommandCandidate } from '~/composables/use-command-trigger';
import type { TriggerKind } from '~/composables/use-input-trigger';
import type { SkillInfo } from '~/composables/bridge/skills';

type Row =
  | { kind: 'skill'; key: string; name: string; label: string; description: string; badge: string }
  | { kind: 'command'; key: string; command: CommandCandidate; label: string; description: string; badge: string };

export interface TriggerSelection {
  kind: 'skill' | 'command';
  name: string;
  label: string;
  /** Custom command body / args; built-ins execute their registry entry. */
  command?: CommandCandidate;
  args?: string;
}

const props = withDefaults(
  defineProps<{
    kind: TriggerKind;
    query: string;
    anchor: { left: number; top: number; width: number } | null;
    sessionId?: string;
  }>(),
  { sessionId: '' }
);

const emit = defineEmits<{
  (e: 'select', value: TriggerSelection): void;
  (e: 'close'): void;
}>();

const { t } = useI18n();
const { skills } = useSkillsList();
const { candidates } = useCommandTrigger();
const loading = ref(false);
const activeIndex = ref(0);
const listRef = useTemplateRef<HTMLElement>('listRef');
const argumentInput = useTemplateRef<{ $el?: HTMLElement }>('argumentInput');
const argumentCommand = ref<CommandCandidate | null>(null);
const argumentValue = ref('');

const rows = computed<Row[]>(() => {
  if (props.kind === 'skill') {
    return (skills.value as SkillInfo[])
      .map(skill => ({ skill, score: scoreTriggerMatch(skill.name, skill.description ?? '', props.query) }))
      .filter(item => item.score >= 0)
      .sort((a, b) => b.score - a.score)
      .map(({ skill }) => ({
        kind: 'skill' as const,
        key: `skill:${skill.name}`,
        name: skill.name,
        label: `$${skill.name}`,
        description: skill.description ?? '',
        badge: skill.category
      }));
  }
  return candidates.value
    .map(command => ({
      command,
      score: scoreTriggerMatch(command.label, command.description, props.query)
    }))
    .filter(item => item.score >= 0)
    .sort((a, b) => b.score - a.score)
    .map(({ command }) => ({
      kind: 'command' as const,
      key: `command:${command.kind}:${command.name}`,
      command,
      label: `/${command.label}`,
      description: command.description,
      badge: command.kind === 'custom' ? t('chatInput.customCommand') : t('chatInput.builtinCommand')
    }));
});

const panelStyle = computed(() => {
  if (!props.anchor) return {};
  const width = Math.min(420, window.innerWidth - 16);
  const left = Math.min(Math.max(8, props.anchor.left), window.innerWidth - width - 8);
  const bottom = Math.max(8, window.innerHeight - props.anchor.top + 8);
  return {
    left: `${left}px`,
    bottom: `${bottom}px`,
    width: `${width}px`
  };
});

/** Scroll the highlighted row into view after a keyboard move. */
async function revealActive(): Promise<void> {
  await nextTick();
  const row = listRef.value?.querySelector<HTMLElement>(`[data-index="${activeIndex.value}"]`);
  row?.scrollIntoView({ block: 'nearest' });
}

/**
 * Move the highlight (wraps at both ends).
 * @param delta
 */
function move(delta: number): void {
  if (!rows.value.length) return;
  activeIndex.value = (activeIndex.value + delta + rows.value.length) % rows.value.length;
  void revealActive();
}

/** Run the highlighted row (the composer routes Enter/click here). */
function selectActive(): void {
  const row = rows.value[activeIndex.value];
  if (row) choose(row);
}

/**
 * Select one row. Custom commands with an argument hint enter argument mode.
 * @param row
 */
function choose(row: Row): void {
  if (row.kind === 'skill') {
    emit('select', { kind: 'skill', name: row.name, label: row.label });
    return;
  }
  const command = row.command;
  if (command.kind === 'custom' && command.argumentHint) {
    argumentCommand.value = command;
    argumentValue.value = '';
    void nextTick(() => {
      const el = argumentInput.value?.$el;
      el?.focus();
    });
    return;
  }
  emit('select', { kind: 'command', name: command.name, label: row.label, command, args: '' });
}

/** Confirm the custom command's arguments. */
function confirmArgument(): void {
  const command = argumentCommand.value;
  if (!command) return;
  emit('select', {
    kind: 'command',
    name: command.name,
    label: `/${command.label}`,
    command,
    args: argumentValue.value.trim()
  });
}

/** Cancel only the argument step; the panel stays open. */
function cancelArgument(): void {
  argumentCommand.value = null;
  argumentValue.value = '';
}

/** Close the panel from the parent's Escape handler. */
function close(): void {
  emit('close');
}

watch(
  () => [props.kind, props.query],
  () => {
    activeIndex.value = 0;
    argumentCommand.value = null;
    argumentValue.value = '';
    void revealActive();
  }
);

onMounted(async () => {
  loading.value = true;
  try {
    if (props.kind === 'skill') await ensureSkillsLoaded();
    else await ensureCommandsLoaded(props.sessionId);
  } finally {
    loading.value = false;
  }
  void nextTick(() => revealActive());
});

defineExpose({ move, selectActive, close });
</script>

<style scoped>
.input-trigger-panel {
  position: fixed;
  /* Above the + Popover's overlay: the panel opens from a menu click while the
     Popover is still dismissing, and a lower layer would swallow its first
     click. PrimeVue's popper layers sit in the 1000+ range. */
  z-index: 1100;
  overflow: hidden;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  background: var(--p-content-background, #fff);
  box-shadow: 0 18px 48px rgb(0 0 0 / 18%);
}
.trigger-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
  font-size: 0.75rem;
  color: var(--p-text-muted-color, #6b7280);
}
.trigger-query {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.trigger-list {
  max-height: 18rem;
  overflow-y: auto;
  padding: 0.25rem;
}
.trigger-row {
  display: flex;
  width: 100%;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.15rem;
  border: 0;
  border-radius: 0.35rem;
  background: transparent;
  padding: 0.45rem 0.6rem;
  text-align: left;
  cursor: pointer;
}
.trigger-row.active,
.trigger-row:hover {
  background: color-mix(in srgb, var(--p-primary-color, #0ea5e9) 14%, transparent);
}
.trigger-name {
  font-size: 0.82rem;
  font-weight: 600;
}
.trigger-badge {
  border-radius: 999px;
  background: var(--p-surface-100, #f3f4f6);
  padding: 0 0.35rem;
  font-size: 0.65rem;
  color: var(--p-text-muted-color, #6b7280);
}
.trigger-desc {
  overflow: hidden;
  width: 100%;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.72rem;
  color: var(--p-text-muted-color, #6b7280);
}
.trigger-empty {
  padding: 1rem 0.75rem;
  text-align: center;
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #6b7280);
}
.trigger-argument {
  display: flex;
  gap: 0.4rem;
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
  padding: 0.5rem;
}
</style>
