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
let optionsTimer: number | null = null;
let renderToken = 0; // monotonically-increasing token; only the latest render may paint
let renderInFlight = false; // true while an async G2 render() has not yet resolved
let frameId: number | null = null;

/**
 * G2 v5 sizing bug (the "marks paint, axis/legend/grid do not" failure):
 * `autoFit` is NOT a Chart-constructor key — it is only honored via the spec
 * passed to `chart.options()`. When the spec's `autoFit:true` is evaluated at
 * the FIRST render, `_computedOptions()` measures the container (which, inside
 * an animating dialog, may still be ~0) and bakes that stale size into
 * `_width/_height`. Geometry marks survive via the coordinate clamp, but the
 * axis/legend/component text region is only re-placed by a fresh
 * `computeLayout` pass — which `canvas.resize()` does NOT trigger because the
 * baked width/height guard sees no *change*. Result: the bar renders, the
 * labels/legend/grid never do.
 *
 * Fix: drop `autoFit` from the spec entirely and drive the chart with
 * EXPLICIT width/height measured from the real, settled container. Options are
 * re-applied on every render with the measured size, and container re-sizes use
 * `chart.changeSize(w, h)` (forceFit() can early-return when bake matches).
 */
let lastSpec: G2Spec | null = null;
let mountedSize = { width: 0, height: 0 }; // latest measured container size

/** Clone the incoming spec with autoFit removed and explicit width/height. */
const buildSpec = (): G2Spec => {
  const rect = containerRef.value?.getBoundingClientRect();
  const width = Math.round(rect?.width ?? mountedSize.width) || mountedSize.width;
  const height = Math.round(rect?.height ?? mountedSize.height) || mountedSize.height;
  // If we have a reliable non-zero measure, remember it for fallback.
  if (width > 0 && height > 0) mountedSize = { width, height };
  const { autoFit: _drop, ...rest } = props.options;
  return { ...rest, width: mountedSize.width, height: mountedSize.height };
};

/**
 * Serialize renders. G2 v5's render() is async and does NOT safely support
 * being invoked again while a previous render is still resolving: a second
 * call clobbers G2's internal state mid-paint, leaving a partial frame
 * (marks painted, axes/legend blank — the exact broken state observed).
 *
 * `render()` coalesces every request into a single rAF-pass and refuses to
 * start a new render until the previous one resolves, so overlapping
 * resize / options-update / mount calls can never race each other inside G2.
 */
let rerenderRequested = false;
const render = async () => {
  if (!chart) {
    rerenderRequested = false;
    return;
  }
  if (renderInFlight) {
    // A render is already running: fold this request in, it will be picked
    // up by the pending render's completion callback.
    rerenderRequested = true;
    return;
  }
  renderInFlight = true;
  const token = ++renderToken;
  try {
    // Always build the spec fresh from the live container size so the plot
    // region (and with it the axis/legend text layer) is laid out at the
    // current real size — never a stale bake.
    lastSpec = buildSpec();
    chart.options(lastSpec);
    await chart.render();
  } catch (e: unknown) {
    // Only surface the newest render's error; stale failures are noise.
    if (token === renderToken) console.error('[GChart] render failed:', e);
  } finally {
    renderInFlight = false;
    // If a resize/options change arrived while we were painting, re-render
    // once more now that the canvas is clear — never drop the request.
    if (!chart) return;
    if (rerenderRequested) {
      rerenderRequested = false;
      // Defer to the next frame so any racy resize settles.
      requestAnimationFrame(render);
    }
  }
};

/** Collapse a burst of resize/options events into a single rAF render. */
const requestRender = () => {
  if (frameId !== null) return;
  frameId = window.requestAnimationFrame(() => {
    frameId = null;
    render();
  });
};

/** When the container changes size, force a real-layout re-render at the new
 *  size (changeSize is unconditional; autoFit would bake the wrong size). */
const scheduleResize = () => {
  if (resizeTimer !== null) return;
  resizeTimer = window.setTimeout(() => {
    resizeTimer = null;
    // Skip zero-sized measurements: G2's component layout (computeCategoryLegendSize
    // → computeHorizontalFlex) crashes on a 0-width container because it reads
    // legend `items[0]` on an as-yet-uncomputed scale. We only ever re-layout at a
    // real, non-zero size.
    const rect = containerRef.value?.getBoundingClientRect();
    if (!rect || Math.round(rect.width) <= 0 || Math.round(rect.height) <= 0) return;
    requestRender();
  }, 120);
};

/** Wait until the container reports a real, non-zero size. PrimeVue Dialog uses a
 *  CSS transition on open; mounting inside it, `getBoundingClientRect()` is ~0
 *  until the animation starts to let go. Rendering G2 at 0×0 makes its component
 *  layout (computeCategoryLegendSize) read `items[0]` of an empty legend scale and
 *  throw — the exact `Cannot read properties of undefined (reading '0')` crash.
 *
 *  Resolve as soon as we observe a real box, or fail over if the component is
 *  torn down (dialog closed) before it ever reaches a non-zero size.
 */
const whenSized = (): Promise<{ width: number; height: number }> =>
  new Promise((resolve, reject) => {
    const startedAt = performance.now();
    const poll = () => {
      const el = containerRef.value;
      if (!el) return reject(new Error('container unmounted'));
      const rect = el.getBoundingClientRect();
      const width = Math.round(rect.width);
      const height = Math.round(rect.height);
      if (width > 0 && height > 0) return resolve({ width, height });
      // Hard cap: never wait forever on a stubbornly 0-sized container.
      if (performance.now() - startedAt > 3000) return reject(new Error('container never sized'));
      requestAnimationFrame(poll);
    };
    poll();
  });

onMounted(async () => {
  if (!containerRef.value) return;
  // Wait for the dialog open animation to settle and the box to be real before
  // creating the chart — rendering at 0×0 triggers the G2 legend-layout crash.
  const size = await whenSized().catch(() => null);
  // The dialog may have started closing/unmounting while we waited — bail out cleanly.
  if (!containerRef.value || !size) return;
  mountedSize = size;
  chart = new Chart({ container: containerRef.value, width: size.width, height: size.height });
  requestRender();
  resizeObserver = new ResizeObserver(scheduleResize);
  resizeObserver.observe(containerRef.value);
});

watch(
  () => props.options,
  () => {
    if (optionsTimer !== null) clearTimeout(optionsTimer);
    optionsTimer = window.setTimeout(() => {
      optionsTimer = null;
      requestRender();
    }, 50);
  },
  { deep: true }
);

onBeforeUnmount(() => {
  if (resizeTimer !== null) clearTimeout(resizeTimer);
  if (optionsTimer !== null) clearTimeout(optionsTimer);
  if (frameId !== null) cancelAnimationFrame(frameId);
  resizeObserver?.disconnect();
  resizeObserver = null;
  renderToken++; // invalidate any in-flight render
  rerenderRequested = false;
  chart?.destroy();
  chart = null;
});
</script>
