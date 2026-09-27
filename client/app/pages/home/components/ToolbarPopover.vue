<template>
  <div
    ref="rootRef"
    class="relative flex items-center">
    <slot
      name="trigger"
      :toggle="toggle"
      :open="open" />

    <!-- Panel: always ABOVE the trigger (these live in the bottom toolbar, where a
         downward panel covers the input box and can run past the window edge), and
         horizontally clamped into the viewport — a trigger near the left edge would
         otherwise push a right-aligned panel out of the window. -->
    <div
      v-if="open"
      ref="panelRef"
      data-test="toolbar-popover"
      role="dialog"
      class="absolute bottom-full z-50 mb-2 max-h-[70vh] overflow-y-auto rounded-lg border border-solid border-gray-100 bg-white p-2 shadow-lg dark:border-gray-700 dark:bg-[#1a1d21]"
      :style="panelStyle"
      @pointerdown.stop>
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { anchorPanelLeft } from '~/utils/toolbar-popover';

const open = ref(false);
const panelRef = ref<HTMLElement>();
const rootRef = ref<HTMLElement>();
/** Inline placement (left offset inside the trigger's wrapper). */
const panelStyle = ref<Record<string, string>>({});

/**
 * Place the panel above the trigger, growing away from the nearer window edge
 * (see `anchorPanelLeft`) and clamped inside the viewport.
 */
const place = () => {
  const wrapper = rootRef.value?.getBoundingClientRect();
  const panel = panelRef.value;
  if (!wrapper || !panel) return;
  const left = anchorPanelLeft({
    triggerLeft: wrapper.left,
    triggerRight: wrapper.right,
    panelWidth: panel.offsetWidth,
    viewportWidth: window.innerWidth
  });
  panelStyle.value = { left: `${left - Math.round(wrapper.left)}px` };
};

/**
 * Toggle the panel. Accepts (and ignores) the click event so callers can bind it
 * straight to a click handler.
 */
const toggle = (): void => {
  open.value = !open.value;
};

/** Close the panel (used after an entry was picked). */
const close = (): void => {
  open.value = false;
};

/**
 * Dismiss when the pointer lands outside both the panel and the trigger.
 * @param event
 */
const onPointerDownOutside = (event: PointerEvent) => {
  const target = event.target as Node | null;
  if (!target) return;
  if (panelRef.value?.contains(target)) return;
  if (rootRef.value?.contains(target)) return;
  close();
};

/**
 * Escape closes the panel, like any other overlay.
 * @param event
 */
const onKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') close();
};

// The listeners only exist while the panel is open.
watch(open, isOpen => {
  if (isOpen) {
    document.addEventListener('pointerdown', onPointerDownOutside, true);
    document.addEventListener('keydown', onKeydown);
    window.addEventListener('resize', place);
    // Place it once the panel has its real width.
    void nextTick(place);
  } else {
    document.removeEventListener('pointerdown', onPointerDownOutside, true);
    document.removeEventListener('keydown', onKeydown);
    window.removeEventListener('resize', place);
  }
});

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onPointerDownOutside, true);
  document.removeEventListener('keydown', onKeydown);
  window.removeEventListener('resize', place);
});

defineExpose({ close, open });
</script>
