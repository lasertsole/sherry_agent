import { defineStore } from 'pinia';
import { ref } from 'vue';
import { DEFAULT_FLOATING_ANCHOR, parseFloatingAnchor, type FloatingAnchor } from '@/common/floating-window';

/**
 * The conversation column's floating presentation: whether the middle column is
 * detached from the shell's flex row, and which edge it snaps to.
 *
 * A global display preference (the column is the same in every session), so it
 * persists across reloads. The position is stored as an ANCHOR, never pixels: a
 * viewport resize re-derives the corner and the window can never be stranded
 * off-screen.
 */
export const useChatFloatStore = defineStore(
  'chatFloat',
  () => {
    /** True when the middle column is shown as a floating window. */
    const floating = ref(false);
    /** The edge the window rests against. */
    const anchor = ref<FloatingAnchor>(DEFAULT_FLOATING_ANCHOR);
    /** User-chosen width in px (0 = the viewport-ratio default). */
    const width = ref(0);
    /** User-chosen height in px (0 = the viewport-ratio default). */
    const height = ref(0);
    /** True when the window is the input-only bar (dragged to the lower bound). */
    const collapsed = ref(false);

    /** Detach the conversation column into the floating window. */
    function open(): void {
      floating.value = true;
    }

    /** Put the conversation column back into the shell's flex row. */
    function dock(): void {
      floating.value = false;
    }

    /** Flip the presentation. */
    function toggle(): void {
      floating.value = !floating.value;
    }

    /**
     * Remember a new snap edge, rejecting values outside the known set (a stale
     * or hand-edited storage row must not break the geometry).
     * @param next The edge reported by the drag release.
     */
    function setAnchor(next: FloatingAnchor): void {
      anchor.value = parseFloatingAnchor(next) ?? DEFAULT_FLOATING_ANCHOR;
    }

    /**
     * Store a resized size; 0 keeps that axis on the ratio default.
     * @param next The size reported by a resize release.
     * @param next.width Width in px (0 = ratio default).
     * @param next.height Height in px (0 = ratio default).
     */
    function setSize(next: { width: number; height: number }): void {
      width.value = Math.max(0, Math.round(next.width));
      height.value = Math.max(0, Math.round(next.height));
    }

    /**
     * Enter or leave the input-only bar.
     * @param next True to collapse, false to expand.
     */
    function setCollapsed(next: boolean): void {
      collapsed.value = next;
    }

    return { floating, anchor, width, height, collapsed, open, dock, toggle, setAnchor, setSize, setCollapsed };
  },
  { persist: { pick: ['floating', 'anchor', 'width', 'height', 'collapsed'] } }
);
