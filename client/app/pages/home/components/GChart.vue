<template>
  <div ref="containerRef" class="w-full h-full" />
</template>

<script lang="ts" setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Chart } from '@antv/g2';
import type { G2Spec } from '@antv/g2';

const props = defineProps<{ options: G2Spec }>();

const containerRef = ref<HTMLDivElement | null>(null);
let chart: Chart | null = null;
let resizeObserver: ResizeObserver | null = null;
let resizeTimer: number | null = null;
let renderSeq = 0;

/**
 * Serialize render invocations so overlapping resize / options-update
 * calls cannot interrupt an in-progress render and leave the marks
 * (e.g. interval bars) blank while axes/legend are already painted.
 */
const render = () => {
  if (!chart) return;
  // Bound the reference to the latest options for this render round.
  const seq = ++renderSeq;
  const opts = props.options;
  chart.options(opts);
  // Guard against a prior AsyncRender promise finishing after a newer
  // render bumped the sequence — never apply a stale paint.
  chart.render().catch((e: unknown) => {
    if (seq === renderSeq) console.error('[GChart] render failed:', e);
  });
};

/** Debounce container size changes: rendering on every ResizeObserver tick
 *  during dialog mount/close animations races G2's internal autoFit and can
 *  leave bars undrawn. Collapse rapid bursts into a single re-render. */
const scheduleResize = () => {
  if (resizeTimer !== null) return;
  resizeTimer = window.setTimeout(() => {
    resizeTimer = null;
    if (chart) render();
  }, 120);
};

onMounted(() => {
  if (!containerRef.value) return;
  chart = new Chart({ container: containerRef.value });
  render();
  resizeObserver = new ResizeObserver(scheduleResize);
  resizeObserver.observe(containerRef.value);
});

let optionsTimer: number | null = null;
watch(
  () => props.options,
  () => {
    if (optionsTimer !== null) clearTimeout(optionsTimer);
    optionsTimer = window.setTimeout(() => {
      optionsTimer = null;
      render();
    }, 50);
  },
  { deep: true }
);

onBeforeUnmount(() => {
  if (resizeTimer !== null) clearTimeout(resizeTimer);
  if (optionsTimer !== null) clearTimeout(optionsTimer);
  resizeObserver?.disconnect();
  resizeObserver = null;
  renderSeq++; // invalidate any in-flight render
  chart?.destroy();
  chart = null;
});
</script>
