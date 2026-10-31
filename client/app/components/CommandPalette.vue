<template>
  <!-- The Ctrl+K command palette: one input over a grouped, ranked command list.
       Every row comes from the registry (pages/home/commands.ts for the static
       half, the skill/session bridges for the dynamic half), so a new command
       never touches this component. -->
  <Dialog
    v-model:visible="visible"
    :modal="true"
    :dismissable-mask="true"
    :show-header="false"
    :style="{ width: '36rem', maxWidth: '92vw' }"
    :pt="{ content: { class: 'p-0!' } }"
    data-test="command-palette"
    @show="onOpen"
    @hide="onClose">
    <div class="border-b border-solid border-gray-light px-3 py-2 dark:border-gray-dark">
      <InputText
        ref="inputRef"
        v-model="query"
        class="w-full border-0! shadow-none!"
        :placeholder="t('commandPalette.placeholder')"
        data-test="command-palette-input"
        @keydown="onKeydown" />
    </div>

    <!-- Empty query: the last few commands the user ran (Dexie). -->
    <div
      v-if="!query.trim() && recentHistory.length"
      class="px-2 py-2"
      data-test="command-palette-recent">
      <div class="px-2 pb-1 text-xs text-gray-400 dark:text-gray-500">{{ t('commandPalette.recent') }}</div>
      <button
        v-for="item in recentHistory"
        :key="item.id"
        type="button"
        class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
        @click="executeById(item.commandId)">
        <i :class="[item.icon || 'pi pi-history', 'text-xs text-gray-400']" />
        <span class="min-w-0 flex-1 truncate">{{ item.label }}</span>
      </button>
    </div>

    <!-- Results, one heading per section. -->
    <div
      v-if="query.trim() || !recentHistory.length"
      ref="listRef"
      class="max-h-[50vh] overflow-y-auto py-1"
      data-test="command-palette-results">
      <template
        v-for="group in groupedResults"
        :key="group.section">
        <div class="px-3 pt-2 pb-1 text-xs text-gray-400 dark:text-gray-500">{{ group.label }}</div>
        <button
          v-for="row in group.rows"
          :key="row.item.entry.id"
          type="button"
          :data-command-id="row.item.entry.id"
          :class="[
            'flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm',
            row.index === activeIndex ? 'bg-theme-main/15 text-theme-main' : 'hover:bg-gray-100 dark:hover:bg-gray-800'
          ]"
          @click="execute(row.item.entry)"
          @mouseenter="activeIndex = row.index">
          <i :class="[row.item.entry.icon || 'pi pi-angle-right', 'text-xs']" />
          <!-- Labels come from i18n or a server-side skill/session name.
               highlightLabel ESCAPES the whole string first and only then wraps
               the match in <mark>, so the only markup is its own; v-safe-html
               is the app-wide second net (DOMPurify) and keeps <mark>. -->
          <span
            class="min-w-0 flex-1 truncate"
            v-safe-html="highlightLabel(row.item.entry.label, query)" />
        </button>
      </template>
      <div
        v-if="!flatResults.length"
        class="px-3 py-6 text-center text-sm text-gray-400 dark:text-gray-500"
        data-test="command-palette-empty">
        {{ t('commandPalette.noMatch') }}
      </div>
    </div>
  </Dialog>
</template>

<script lang="ts" setup>
import { computed, nextTick, ref, useTemplateRef, watch } from 'vue';
import { useI18n } from 'vue-i18n';

// The registry / history / bridge helpers are Nuxt auto-imports (bare
// symbols); only the types are imported explicitly.
import type { CommandEntry, CommandSection, ScoredCommand } from '~/composables/use-command-registry';
import type { CommandHistoryEntry } from '~/composables/db';

const visible = defineModel<boolean>('visible', { default: false });

/** The palette cannot open a tab itself; the shell owns the right sidebar. */
const emit = defineEmits<{ 'open-tab': [kind: 'skills'] }>();

const { t } = useI18n();
const router = useRouter();
const localePath = useLocalePath();

const query = ref('');
const activeIndex = ref(0);
const recentHistory = ref<CommandHistoryEntry[]>([]);
const inputRef = useTemplateRef<{ $el?: HTMLElement }>('inputRef');
const listRef = useTemplateRef<HTMLElement>('listRef');

const all = useAllCommands();

/** Typing a new query puts the cursor back on the best match (index 0). */
watch(query, () => {
  activeIndex.value = 0;
});

/** Sections in render order (sessions and skills first: they are the targets). */
const SECTIONS: ReadonlyArray<CommandSection> = ['session', 'navigate', 'tools', 'skills'];

/** Ranked matches, then grouped into the sections above with a flat index each. */
const groupedResults = computed(() => {
  const matched = searchCommands(query.value, all.value);
  let index = 0;
  return SECTIONS.map(section => {
    const items = matched.filter(scored => scored.entry.section === section);
    const rows = items.map(item => ({ item, index: index++ }));
    return { section, label: t(`commandPalette.section.${section}`), rows };
  }).filter(group => group.rows.length > 0);
});

/** Every matched command as one flat list (the keyboard cursor walks this). */
const flatResults = computed<ScoredCommand[]>(() =>
  groupedResults.value.flatMap(group => group.rows.map(row => row.item))
);

/**
 * Execute one command: close first, then run + record (a slow handler must not keep the panel up).
 * @param entry
 */
function execute(entry: CommandEntry): void {
  visible.value = false;
  void Promise.resolve(entry.handler()).catch(() => {
    // A failed command is the handler's own concern (it toasts); the palette just closes.
  });
  void recordCommandHistory(entry);
}

/**
 * Run a history row's command IF it is still registered (a deleted skill merely vanishes).
 * @param commandId
 */
function executeById(commandId: string): void {
  const entry = all.value.find(candidate => candidate.id === commandId);
  if (entry) execute(entry);
}

/**
 * Keep the keyboard cursor inside the list and follow it with the scroll.
 * @param event
 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'ArrowDown') {
    event.preventDefault();
    activeIndex.value = Math.min(activeIndex.value + 1, Math.max(flatResults.value.length - 1, 0));
    scrollActiveIntoView();
    return;
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault();
    activeIndex.value = Math.max(activeIndex.value - 1, 0);
    scrollActiveIntoView();
    return;
  }
  if (event.key === 'Enter') {
    event.preventDefault();
    const item = flatResults.value[activeIndex.value];
    if (item) execute(item.entry);
    return;
  }
  if (event.key === 'Escape') {
    event.preventDefault();
    visible.value = false;
  }
}

/** Scroll the highlighted row into view (nearest: the list does not jump). */
function scrollActiveIntoView(): void {
  void nextTick(() => {
    const container = listRef.value;
    const active = container?.querySelectorAll<HTMLElement>('[data-command-id]')[activeIndex.value];
    active?.scrollIntoView({ block: 'nearest' });
  });
}

/** Refresh the dynamic half (skills + recent sessions) and the history. */
async function refreshDynamic(): Promise<void> {
  const [skills, sessions] = await Promise.all([
    listSkills()
      .then(result => result.skills)
      .catch(() => []),
    getSessionList().catch(() => [])
  ]);
  const skillEntries: CommandEntry[] = skills.map(skill => ({
    id: `skill.${skill.name}`,
    label: skill.name,
    section: 'skills',
    icon: 'pi pi-bolt',
    keywords: [skill.name, skill.description, skill.category],
    // The palette's skill commands open the skill manager; activating a single
    // skill from here is a later step (it needs the same confirmation the panel uses).
    handler: () => openSkillPanel()
  }));
  const sessionEntries: CommandEntry[] = sessions.slice(0, 20).map(session => ({
    id: `session.switch.${session.id}`,
    label: session.title || session.id,
    section: 'session',
    icon: 'pi pi-comments',
    keywords: [session.title, session.id],
    handler: () => {
      void router.push(localePath(`/home/${session.id}`));
    }
  }));
  setDynamicCommands([...sessionEntries, ...skillEntries]);
}

/** Open the skill manager (the shell owns the tab; the palette only clicks it). */
function openSkillPanel(): void {
  emit('open-tab', 'skills');
}

/** Dialog shown: reset the state, pull the live halves, focus the input. */
async function onOpen(): Promise<void> {
  query.value = '';
  activeIndex.value = 0;
  void nextTick(() => {
    const el = (inputRef.value as unknown as { $el?: HTMLElement } | null)?.$el;
    el?.focus();
  });
  const [, history] = await Promise.all([refreshDynamic(), recentCommandHistory(5)]);
  recentHistory.value = history;
}

/** Dialog closed: the next open starts clean. */
function onClose(): void {
  query.value = '';
  activeIndex.value = 0;
}
</script>
