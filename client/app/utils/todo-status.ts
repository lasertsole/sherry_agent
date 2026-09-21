/**
 * Todo status → icon class mapping (TodoItem).
 *
 * Extracted from `TodoItem.vue`'s `statusIcon` switch into a lookup table; the
 * default branch (any non-terminal status) keeps its exact former icon. The
 * values are frozen presentation data pinned by
 * `utils/__tests__/todo-status.test.ts`.
 *
 * @module utils/todo-status
 */
import type { TodoStatus } from '~/stores/todo';

/** Icon classes per explicit todo status. */
export const TODO_STATUS_ICON: Record<string, string> = {
  completed: 'pi pi-check-circle text-green-500',
  cancelled: 'pi pi-times-circle text-color-secondary',
  in_progress: 'pi pi-spin pi-spinner text-primary'
};

/** Icon shown for `pending` and any unknown status (former `default` branch). */
export const TODO_STATUS_ICON_FALLBACK = 'pi pi-circle text-color-secondary';

/**
 * Resolve the icon class for a todo status.
 * @param status
 */
export function resolveTodoStatusIcon(status: TodoStatus | string | null | undefined): string {
  return TODO_STATUS_ICON[status ?? ''] ?? TODO_STATUS_ICON_FALLBACK;
}
