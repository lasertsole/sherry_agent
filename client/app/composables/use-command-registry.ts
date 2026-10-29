import { computed, ref, type ComputedRef } from 'vue';

/**
 * The app's command registry (the Ctrl+K palette's data model).
 *
 * Two sources, mirroring how the shell already registers commands:
 * * **static** — the 菜单 grid, the toolbox entries and "New chat", registered
 *   once by the home shell (`pages/home/commands.ts`);
 * * **dynamic** — the skill index and the recent sessions, refreshed every time
 *   the palette opens (they change while the app runs).
 *
 * A command is a plain record: id (unique, also the history key), label, section,
 * optional icon/keywords, and the handler it runs. Adding a command never touches
 * the palette component (F2) — the panel only renders whatever is registered.
 */

/** Group a command belongs to (the palette renders one heading per section). */
export type CommandSection = 'session' | 'navigate' | 'tools' | 'skills';

/** One runnable entry in the palette. */
export interface CommandEntry {
  /** Stable unique id (also the search-history key), e.g. `toolbar.skills`. */
  id: string;
  /** Text the user sees and the matcher scores. */
  label: string;
  /** Which heading the entry renders under. */
  section: CommandSection;
  /** PrimeVue icon class (`pi pi-bolt`). */
  icon?: string;
  /** Extra match terms not shown in the label (the event name, a skill category…). */
  keywords?: string[];
  /** Runs the command. Errors are the handler's problem; the panel closes first. */
  handler: () => void | Promise<void>;
}

/** One matched entry plus its score (higher first). */
export interface ScoredCommand {
  entry: CommandEntry;
  score: number;
}

const staticCommands = ref<CommandEntry[]>([]);
const dynamicCommands = ref<CommandEntry[]>([]);

/**
 * Register one static command. A repeated id REPLACES the earlier entry (the
 * shell re-registers on every mount; a hot reload must not duplicate rows).
 * @param entry Command to add.
 */
export function registerStatic(entry: CommandEntry): void {
  const next = staticCommands.value.filter(candidate => candidate.id !== entry.id);
  next.push(entry);
  staticCommands.value = next;
}

/**
 * Register several static commands at once (same replace-by-id rule).
 * @param entries Commands to add.
 */
export function registerStaticBatch(entries: CommandEntry[]): void {
  const ids = new Set(entries.map(entry => entry.id));
  const kept = staticCommands.value.filter(candidate => !ids.has(candidate.id));
  staticCommands.value = [...kept, ...entries];
}

/**
 * Replace the dynamic half (skills / sessions). The palette refreshes it on open,
 * so a stale list never outlives the panel being closed.
 * @param entries Commands discovered at open time.
 */
export function setDynamicCommands(entries: CommandEntry[]): void {
  dynamicCommands.value = entries;
}

/** Every registered command: static first, then dynamic (stable display order). */
export function allCommands(): CommandEntry[] {
  return [...staticCommands.value, ...dynamicCommands.value];
}

/** Reactive view of the whole registry (the palette's source list). */
export function useAllCommands(): ComputedRef<CommandEntry[]> {
  return computed(() => allCommands());
}

/** For tests: forget every registration. */
export function resetCommandRegistry(): void {
  staticCommands.value = [];
  dynamicCommands.value = [];
}

/**
 * Score one entry against a lowercased query.
 *
 * The scale is deliberately coarse: an exact label beats a prefix, a prefix
 * beats a substring, and keyword hits sit below every label hit. Within a tier,
 * a shorter/earlier match wins — that is the whole "fuzzy" part (no subsequence
 * matching, no pinyin: four UI languages make a character-level scheme a
 * different feature).
 * @param entry Candidate command.
 * @param q Lowercased, trimmed query ('' matches everything).
 * @returns Score, or -1 when the entry does not match.
 */
export function scoreCommand(entry: CommandEntry, q: string): number {
  if (!q) return 1;
  const label = entry.label.toLowerCase();
  if (label === q) return 1000;
  if (label.startsWith(q)) return 500 - Math.min(label.length, 100);
  const at = label.indexOf(q);
  if (at >= 0) return 300 - Math.min(at, 100);
  const keywords = (entry.keywords ?? []).map(keyword => keyword.toLowerCase());
  if (keywords.some(keyword => keyword === q)) return 200;
  if (keywords.some(keyword => keyword.startsWith(q))) return 150;
  if (keywords.some(keyword => keyword.includes(q))) return 50;
  return -1;
}

/**
 * Filter + rank the command list for a query.
 * @param query Raw user input.
 * @param commands Candidate list (usually {@link allCommands}).
 * @returns Matches, best first; `[]` when nothing matches (never throws).
 */
export function searchCommands(query: string, commands: CommandEntry[]): ScoredCommand[] {
  const q = String(query ?? '')
    .trim()
    .toLowerCase();
  return commands
    .map(entry => ({ entry, score: scoreCommand(entry, q) }))
    .filter(scored => scored.score >= 0)
    .sort((a, b) => b.score - a.score);
}

/**
 * HTML-escape one string (the palette highlights with v-html).
 * @param text
 */
export function escapeHtml(text: string): string {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * The label with its matched substring wrapped in `<mark>`.
 *
 * Escaping happens FIRST and per segment, so user/page-provided text (a session
 * title, a skill description) can never inject markup through the highlight.
 * @param label Entry label (raw).
 * @param query Raw user input; empty returns the escaped label unchanged.
 * @returns Safe HTML for `v-html`.
 */
export function highlightLabel(label: string, query: string): string {
  const text = String(label ?? '');
  const q = String(query ?? '').trim();
  if (!q) return escapeHtml(text);
  const at = text.toLowerCase().indexOf(q.toLowerCase());
  if (at < 0) return escapeHtml(text);
  const before = escapeHtml(text.slice(0, at));
  const match = escapeHtml(text.slice(at, at + q.length));
  const after = escapeHtml(text.slice(at + q.length));
  return `${before}<mark>${match}</mark>${after}`;
}
