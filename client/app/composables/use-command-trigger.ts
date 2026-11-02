/**
 * Slash-command candidates for the composer's `/` picker.
 *
 * Two sources: user/workspace Markdown commands (`GET /commands`) and the
 * app's existing command registry. Custom commands insert a prompt chip;
 * built-ins run immediately, exactly like the Ctrl+K palette.
 *
 * @module composables/use-command-trigger
 */

import { computed, shallowRef } from 'vue';

import type { CustomCommand } from './bridge/commands';
import type { CommandEntry } from './use-command-registry';
// Stable specifier: the bridge value is mocked by tests.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { listCommands } from './bridge/commands';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { allCommands } from './use-command-registry';

/** One row in the slash picker. */
export interface CommandCandidate {
  kind: 'custom' | 'builtin';
  /** Stable id (`review` / `toolbar.skills`). */
  name: string;
  /** Visible label without the leading slash. */
  label: string;
  description: string;
  /** Custom command's optional argument placeholder. */
  argumentHint?: string;
  /** Custom command's prompt template. */
  body?: string;
  /** Built-in registry entry (its handler runs on selection). */
  entry?: CommandEntry;
}

const custom = shallowRef<CustomCommand[]>([]);
const loadedFor = shallowRef<string | null>(null);
let inflight: Promise<void> | null = null;

/**
 * Load the custom command list for one session. Workspace commands depend on
 * the session's project root, so a different session re-reads the API.
 * @param sessionId Current session (empty keeps user scope only).
 */
export async function ensureCommandsLoaded(sessionId = ''): Promise<void> {
  if (loadedFor.value === sessionId) return;
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      custom.value = (await listCommands(sessionId || undefined)).commands ?? [];
      loadedFor.value = sessionId;
    } catch {
      custom.value = [];
      loadedFor.value = sessionId;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

function toBuiltin(entry: CommandEntry): CommandCandidate {
  return {
    kind: 'builtin',
    name: entry.id,
    label: entry.label,
    description: entry.keywords?.join(' · ') ?? '',
    entry
  };
}

function toCustom(command: CustomCommand): CommandCandidate {
  return {
    kind: 'custom',
    name: command.name,
    label: command.name,
    description: command.description,
    argumentHint: command.argument_hint,
    body: command.body
  };
}

/** All candidates: custom commands first, then registered app commands. */
export function commandCandidates(): CommandCandidate[] {
  return [...custom.value.map(toCustom), ...allCommands().map(toBuiltin)];
}

/** Reactive view for the picker. */
export function useCommandTrigger() {
  return {
    candidates: computed(() => commandCandidates())
  };
}
