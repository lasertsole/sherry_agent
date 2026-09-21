import { reactive } from 'vue';

/** Open/close actions + per-id visibility flags for one set of dialogs. */
export interface DialogManager<Id extends string> {
  /**
   * Visibility flag per registered id. Safe to bind directly with
   * `v-if` / `v-model` on the dialog component.
   */
  visible: Record<Id, boolean>;
  /** Whether the dialog is currently open. */
  isOpen: (id: Id) => boolean;
  /** Open the dialog. */
  open: (id: Id) => void;
  /** Close the dialog. */
  close: (id: Id) => void;
  /** Flip the dialog's visibility. */
  toggle: (id: Id) => void;
}

/**
 * Registry-driven dialog visibility manager.
 *
 * Replaces the ad-hoc `const showXxxDialog = ref(false)` cluster: a page hands
 * in the ids of every dialog it owns once, and the returned flags/actions cover
 * them (`v-if="dialogs.visible.skills" v-model="dialogs.visible.skills"`).
 * Adding a dialog is then a matter of extending the id list, not of adding
 * another ref and another switch case.
 * @param ids Concrete dialog ids owned by the caller (initialized closed).
 * @returns The visibility flags plus the open/close/toggle actions.
 */
export function useDialogManager<Id extends string>(ids: readonly Id[]): DialogManager<Id> {
  const visible = reactive<Record<string, boolean>>({});
  for (const id of ids) visible[id] = false;

  return {
    visible,
    isOpen: (id: Id): boolean => visible[id] === true,
    open: (id: Id): void => {
      visible[id] = true;
    },
    close: (id: Id): void => {
      visible[id] = false;
    },
    toggle: (id: Id): void => {
      visible[id] = !visible[id];
    }
  };
}
