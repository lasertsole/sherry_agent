/**
 * Session-page toolbar command registry (`index/[sid].vue`).
 *
 * The page used to dispatch its toolbar with a 4-case `switch` over
 * `handleOperate` (`createSession` / `uploadImage` / `uploadAudio` /
 * `uploadVideo`). It is now a data registry: `SESSION_TOOLBAR_EVENTS` is the
 * vocabulary and `buildSessionToolbarCommands` maps each event to the command it
 * runs. The internal `Record<SessionToolbarEvent, …>` makes a missing entry a
 * compile error, while the returned index-signature type keeps the dispatcher
 * assertion-free for unknown events.
 */

/** Toolbar event vocabulary of the session page (former switch cases). */
export const SESSION_TOOLBAR_EVENTS = ['createSession', 'uploadImage', 'uploadAudio', 'uploadVideo'] as const;

/** One session-page toolbar event. */
export type SessionToolbarEvent = (typeof SESSION_TOOLBAR_EVENTS)[number];

/** Commands the session toolbar delegates to (owned by the page). */
export interface SessionToolbarHandlers {
  createSession: () => void;
  uploadImage: () => void;
  uploadAudio: () => void;
  uploadVideo: () => void;
}

/**
 * Build the session toolbar command registry.
 * @param handlers Page actions the commands delegate to.
 * @returns Event → command table (unknown events resolve to `undefined`).
 */
export function buildSessionToolbarCommands(handlers: SessionToolbarHandlers): Record<string, () => void> {
  const commands: Record<SessionToolbarEvent, () => void> = {
    createSession: () => handlers.createSession(),
    uploadImage: () => handlers.uploadImage(),
    uploadAudio: () => handlers.uploadAudio(),
    uploadVideo: () => handlers.uploadVideo()
  };
  return commands;
}
