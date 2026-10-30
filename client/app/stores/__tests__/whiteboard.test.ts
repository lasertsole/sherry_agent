/**
 * The whiteboard store (unit).
 *
 * One board per key, in memory: a stroke commits and clears the redo stack,
 * undo/redo move strokes between the two stacks, clear wipes both, and the
 * stroke cap keeps a runaway scribble bounded. A board survives its panel
 * (closing the tab is not a delete) — the store simply keeps it.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useWhiteboardStore, WHITEBOARD_MAX_STROKES } from '@/stores/whiteboard';
import type { WhiteboardStroke } from '@/composables/use-whiteboard-canvas';

function stroke(color = '#1f1f1f'): WhiteboardStroke {
  return { tool: 'pen', color, width: 4, points: [{ x: 0.1, y: 0.1 }] };
}

describe('useWhiteboardStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('creates an empty board on first use and keeps boards apart', () => {
    const store = useWhiteboardStore();

    expect(store.ensureBoard('a').strokes).toEqual([]);
    store.addStroke('a', stroke());
    store.addStroke('b', stroke());

    expect(store.ensureBoard('a').strokes).toHaveLength(1);
    expect(store.ensureBoard('b').strokes).toHaveLength(1);
    expect(store.ensureBoard('a').strokes).not.toBe(store.ensureBoard('b').strokes);
  });

  it('commits strokes and clears the redo stack on a new stroke', () => {
    const store = useWhiteboardStore();
    store.addStroke('a', stroke('#111111'));
    store.addStroke('a', stroke('#222222'));
    store.undo('a');

    store.addStroke('a', stroke('#333333'));

    const board = store.ensureBoard('a');
    expect(board.strokes.map(item => item.color)).toEqual(['#111111', '#333333']);
    expect(board.undoneStrokes).toEqual([]);
  });

  it('undo and redo move one stroke between the stacks', () => {
    const store = useWhiteboardStore();
    store.addStroke('a', stroke('#111111'));
    store.addStroke('a', stroke('#222222'));

    store.undo('a');
    expect(store.ensureBoard('a').strokes.map(item => item.color)).toEqual(['#111111']);
    expect(store.ensureBoard('a').undoneStrokes.map(item => item.color)).toEqual(['#222222']);

    store.redo('a');
    expect(store.ensureBoard('a').strokes.map(item => item.color)).toEqual(['#111111', '#222222']);
    expect(store.ensureBoard('a').undoneStrokes).toEqual([]);
  });

  it('undo/redo on an empty board are no-ops', () => {
    const store = useWhiteboardStore();

    store.undo('a');
    store.redo('a');

    expect(store.ensureBoard('a').strokes).toEqual([]);
    expect(store.ensureBoard('a').undoneStrokes).toEqual([]);
  });

  it('clear wipes the strokes AND the redo history', () => {
    const store = useWhiteboardStore();
    store.addStroke('a', stroke());
    store.undo('a');
    store.addStroke('a', stroke());

    store.clear('a');

    expect(store.ensureBoard('a').strokes).toEqual([]);
    expect(store.ensureBoard('a').undoneStrokes).toEqual([]);
  });

  it('renames and deletes a board', () => {
    const store = useWhiteboardStore();
    store.renameBoard('a', 'My sketch');
    expect(store.ensureBoard('a').name).toBe('My sketch');

    store.deleteBoard('a');
    expect(store.boards.a).toBeUndefined();
  });

  it('stops at the stroke cap instead of growing forever', () => {
    const store = useWhiteboardStore();
    const board = store.ensureBoard('a');
    board.strokes = Array.from({ length: WHITEBOARD_MAX_STROKES }, () => stroke());

    expect(store.addStroke('a', stroke())).toBe(false);
    expect(store.ensureBoard('a').strokes).toHaveLength(WHITEBOARD_MAX_STROKES);
  });
});
