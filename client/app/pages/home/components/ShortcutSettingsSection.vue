<template>
  <!-- Shortcut bindings (菜单 → 系统设置 → 快捷键). Every row shows the chord in
       force, a 重绑 button that captures the next key press, and a reset once the
       user has overridden it. Overrides live in Dexie and apply immediately. -->
  <div class="flex flex-col gap-4">
    <p class="m-0 text-xs text-gray-500 dark:text-gray-400">
      {{ t('shortcuts.hint') }}
    </p>

    <section
      v-for="category in categories"
      :key="category"
      class="flex flex-col gap-1">
      <h4 class="m-0 text-xs font-semibold text-gray-600 dark:text-gray-300">
        {{ t(`shortcuts.group.${category}`) }}
      </h4>
      <div
        v-for="binding in byCategory(category)"
        :key="binding.id"
        class="flex items-center gap-2 rounded-md border border-solid border-gray-100 px-2 py-1.5 dark:border-gray-800"
        :data-binding-id="binding.id">
        <span class="min-w-0 flex-1 truncate text-xs text-gray-700 dark:text-gray-200">
          {{ t(binding.label) }}
        </span>
        <kbd
          class="shrink-0 rounded border border-solid border-gray-300 bg-gray-50 px-1.5 py-0.5 font-mono text-[0.7rem] text-gray-700 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
          data-test="shortcut-combo">
          {{ rebindingId === binding.id ? t('shortcuts.listening') : formatCombo(currentKeys(binding.id)) }}
        </kbd>
        <Button
          :label="rebindingId === binding.id ? t('shortcuts.cancelRebind') : t('shortcuts.rebind')"
          size="small"
          text
          :severity="rebindingId === binding.id ? 'danger' : 'secondary'"
          :data-test="`shortcut-rebind-${binding.id}`"
          @click="toggleRebind(binding.id)" />
        <Button
          v-if="hasOverride(binding.id)"
          icon="pi pi-replay"
          size="small"
          text
          severity="secondary"
          :title="t('shortcuts.reset')"
          :aria-label="t('shortcuts.reset')"
          :data-test="`shortcut-reset-${binding.id}`"
          @click="reset(binding.id)" />
      </div>
    </section>

    <p
      v-if="conflict"
      class="m-0 rounded-md bg-red-500/10 px-2 py-1 text-xs text-red-500"
      data-test="shortcut-conflict">
      {{ t('shortcuts.conflict', { name: t(conflict.label) }) }}
    </p>

    <div class="flex justify-end">
      <Button
        :label="t('shortcuts.resetAll')"
        size="small"
        text
        severity="secondary"
        :disabled="!hasAnyOverride"
        data-test="shortcut-reset-all"
        @click="resetAll" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';

import type { KeyCombo, ShortcutBinding, ShortcutCategory } from '~/composables/use-shortcuts';

/**
 * The shortcut-bindings section of the system settings.
 *
 * Capture is a WINDOW CAPTURE-PHASE listener while a row is being rebound, so
 * the pressed chord never reaches the app's own dispatcher (the palette would
 * otherwise open on the Ctrl+K the user is trying to record) — it is consumed
 * with `stopImmediatePropagation` and only our handler sees it.
 */
const { t } = useI18n();
const shortcuts = useShortcuts();

const categories: ShortcutCategory[] = ['composer', 'navigation', 'global'];

/** The binding whose next key press is being recorded (null = idle). */
const rebindingId = ref<string | null>(null);
/** The binding the last rejected candidate collided with. */
const conflict = ref<ShortcutBinding | null>(null);

/**
 * Bindings of one category, in table order.
 * @param category
 */
const byCategory = (category: ShortcutCategory): readonly ShortcutBinding[] =>
  shortcuts.bindings.filter(binding => binding.category === category);

/**
 * The chord in force for one binding (override ?? default).
 * @param id
 */
const currentKeys = (id: string): KeyCombo =>
  shortcuts.effectiveKeys(id) ?? shortcuts.bindings.find(b => b.id === id)!.defaultKeys;

/**
 * Whether the user overrode this binding (drives the reset button).
 * @param id
 */
const hasOverride = (id: string): boolean => Boolean(shortcuts.overrides[id]);

/** Whether any override exists (drives 全部恢复默认). */
const hasAnyOverride = computed(() => Object.keys(shortcuts.overrides).length > 0);

/**
 * Consume the captured chord: Esc cancels, a bare modifier waits for the real
 * key, a conflict is reported WITHOUT saving, anything else is persisted.
 * @param event The window capture-phase keydown.
 */
const onCaptureKeydown = (event: KeyboardEvent): void => {
  if (!rebindingId.value) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (event.key === 'Escape') {
    rebindingId.value = null;
    conflict.value = null;
    return;
  }
  // A modifier alone is a half-typed chord: keep waiting.
  if (['shift', 'control', 'alt', 'meta'].includes(event.key.toLowerCase())) return;
  const combo = comboFromEvent(event);
  const hit = shortcuts.findConflict(combo, rebindingId.value);
  if (hit) {
    conflict.value = hit;
    return;
  }
  void shortcuts.save(rebindingId.value, combo);
  rebindingId.value = null;
  conflict.value = null;
};

/**
 * Start/stop recording for one binding.
 * @param id Binding id.
 */
const toggleRebind = (id: string): void => {
  conflict.value = null;
  rebindingId.value = rebindingId.value === id ? null : id;
};

/**
 * Restore one binding's built-in default.
 * @param id Binding id.
 */
const reset = (id: string): void => {
  conflict.value = null;
  if (rebindingId.value === id) rebindingId.value = null;
  void shortcuts.reset(id);
};

/** Restore every binding. */
const resetAll = (): void => {
  conflict.value = null;
  rebindingId.value = null;
  for (const id of Object.keys(shortcuts.overrides)) void shortcuts.reset(id);
};

// The capture listener exists only while a row is recording.
watch(rebindingId, id => {
  if (id) window.addEventListener('keydown', onCaptureKeydown, true);
  else window.removeEventListener('keydown', onCaptureKeydown, true);
});

onBeforeUnmount(() => window.removeEventListener('keydown', onCaptureKeydown, true));
</script>
