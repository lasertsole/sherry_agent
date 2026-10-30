import { defineStore } from 'pinia';
import { ref } from 'vue';
import type { WhiteboardStroke } from '~/composables/use-whiteboard-canvas';

/**
 * The whiteboard's boards, keyed by board id (one board per right-sidebar tab
 * payload; `default` when a tab carries none).
 *
 * In-memory by design: a reload starts with empty boards. Closing the tab does
 * NOT delete the board — reopening it restores the same strokes, because the
 * store outlives the panel (the same contract the terminal's scrollback keeps).
 * Undo/redo is a two-stack model: a new stroke clears the redo stack.
 */

/** One board's strokes and its (small) identity. */
export interface WhiteboardBoard {
  strokes: WhiteboardStroke[];
  undoneStrokes: WhiteboardStroke[];
  name: string;
}

/** How many strokes one board keeps (a runaway scribble must not grow forever). */
export const WHITEBOARD_MAX_STROKES = 5_000;

/** A fresh, empty board. */
function emptyBoard(): WhiteboardBoard {
  return { strokes: [], undoneStrokes: [], name: '' };
}

export const useWhiteboardStore = defineStore('whiteboard', () => {
  const boards = ref<Record<string, WhiteboardBoard>>({});

  /**
   * The board for one id, created on first use.
   * @param boardId Board key.
   */
  function ensureBoard(boardId: string): WhiteboardBoard {
    if (!boards.value[boardId]) boards.value[boardId] = emptyBoard();
    return boards.value[boardId];
  }

  /**
   * Commit one finished stroke and drop the redo history.
   * @param boardId Board key.
   * @param stroke Finished stroke.
   * @returns Whether the stroke was kept (false at the cap).
   */
  function addStroke(boardId: string, stroke: WhiteboardStroke): boolean {
    const board = ensureBoard(boardId);
    if (board.strokes.length >= WHITEBOARD_MAX_STROKES) return false;
    board.strokes.push(stroke);
    board.undoneStrokes = [];
    return true;
  }

  /**
   * Undo the last committed stroke.
   * @param boardId
   */
  function undo(boardId: string): void {
    const board = ensureBoard(boardId);
    const stroke = board.strokes.pop();
    if (stroke) board.undoneStrokes.push(stroke);
  }

  /**
   * Redo the most recently undone stroke.
   * @param boardId
   */
  function redo(boardId: string): void {
    const board = ensureBoard(boardId);
    const stroke = board.undoneStrokes.pop();
    if (stroke) board.strokes.push(stroke);
  }

  /**
   * Clear the board (undo cannot resurrect a cleared board).
   * @param boardId
   */
  function clear(boardId: string): void {
    const board = ensureBoard(boardId);
    board.strokes = [];
    board.undoneStrokes = [];
  }

  /**
   * Rename the board (display only: the PNG export's file name).
   * @param boardId
   * @param name
   */
  function renameBoard(boardId: string, name: string): void {
    ensureBoard(boardId).name = name;
  }

  /**
   * Forget one board entirely.
   * @param boardId
   */
  function deleteBoard(boardId: string): void {
    delete boards.value[boardId];
  }

  return { boards, ensureBoard, addStroke, undo, redo, clear, renameBoard, deleteBoard };
});
