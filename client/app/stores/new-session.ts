import { defineStore } from 'pinia';

/**
 * New-session flow state: the mandatory preset-choice dialog.
 *
 * Every 新建对话 entry point (the left sidebar button, the chat-area empty
 * state, the session-toolbar command) opens this dialog instead of creating a
 * session directly — a session cannot exist without a chosen preset, and the
 * dialog applies the chosen preset (persona files + character) before the new
 * session locks its own prompt snapshot.
 */
export const useNewSessionStore = defineStore('newSession', () => {
  /** Whether the preset-choice dialog is open. */
  const dialogOpen = ref(false);

  /** Open the dialog (any 新建对话 entry point calls this). */
  function openDialog(): void {
    dialogOpen.value = true;
  }

  /** Close the dialog without creating anything. */
  function closeDialog(): void {
    dialogOpen.value = false;
  }

  return { dialogOpen, openDialog, closeDialog };
});
