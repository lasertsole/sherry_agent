/**
 * Workspace-file drop zone of the chat composer.
 *
 * A file dragged from the sidebar / file tree carries a JSON
 * :type:`FileMentionPayload` under `FILE_MENTION_MIME`; dropping it inserts the
 * same `@path` mention chip the "Add to chat" action does (never the file's
 * content — the agent reads the file itself). An OS file drop is a different
 * feature (attachment upload) and must not light up this surface.
 *
 * The root element stays in the component that owns the composer: this is the
 * event/highlight state only, so the `.drag-over` contract and the flex layout
 * are untouched.
 */
import { ref } from 'vue';
import type { FileMentionPayload } from '~/composables/use-file-mention';

/** Collaborators of the composer drop zone. */
export interface ComposerFileDropOptions {
  /** Insert the dropped reference (the composer builds a mention chip). */
  insert: (payload: FileMentionPayload) => void;
}

/**
 * Create the composer drop-zone state.
 * @param options
 */
export function useComposerFileDrop(options: ComposerFileDropOptions) {
  /** True while a workspace file is dragged over the composer (highlight hint). */
  const isDragOver = ref(false);

  /**
   * Highlight only for a workspace-file drag.
   * @param event
   */
  function onDragOver(event: DragEvent): void {
    if (!event.dataTransfer?.types.includes(FILE_MENTION_MIME)) return;
    isDragOver.value = true;
    event.dataTransfer.dropEffect = 'copy';
  }

  /** The pointer left the composer: drop the highlight. */
  function onDragLeave(): void {
    isDragOver.value = false;
  }

  /**
   * Drop a workspace file onto the composer: insert its mention chip at the
   * caret.
   * @param event
   */
  function onDrop(event: DragEvent): void {
    isDragOver.value = false;
    const raw = event.dataTransfer?.getData(FILE_MENTION_MIME);
    if (!raw) return;
    try {
      const payload = JSON.parse(raw) as FileMentionPayload;
      if (payload?.path) options.insert(payload);
    } catch {
      // A malformed payload (another app's drag) is ignored, not fatal.
    }
  }

  return { isDragOver, onDragOver, onDragLeave, onDrop };
}
