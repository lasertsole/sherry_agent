<template>
  <!-- Desktop-only: the Rust shell prevents the default close and emits
       `close-requested`; this dialog is the "ask once" path. Hidden in the
       browser build (no window API). -->
  <Dialog
    v-model:visible="visible"
    :header="t('closeTray.title')"
    :modal="true"
    :closable="true"
    :dismissable-mask="false"
    class="w-[min(92vw,26rem)]"
    data-test="close-tray-dialog">
    <div class="flex flex-col gap-3">
      <p class="m-0 text-sm leading-relaxed text-gray-700 dark:text-gray-300">
        {{ t('closeTray.message') }}
      </p>
      <div class="flex items-center gap-2">
        <Checkbox
          v-model="remember"
          input-id="close-tray-remember"
          binary />
        <label
          for="close-tray-remember"
          class="text-sm cursor-pointer text-gray-600 dark:text-gray-400">
          {{ t('closeTray.remember') }}
        </label>
      </div>
    </div>
    <template #footer>
      <div class="flex justify-end gap-2">
        <Button
          :label="t('closeTray.quit')"
          severity="secondary"
          text
          data-test="close-tray-quit"
          @click="choose('exit')" />
        <Button
          :label="t('closeTray.minimize')"
          data-test="close-tray-minimize"
          @click="choose('tray')" />
      </div>
    </template>
  </Dialog>
</template>

<script lang="ts" setup>
/**
 * Close-to-tray prompt (desktop app only).
 *
 * The Rust shell intercepts the window's close button, prevents the default
 * exit and emits `close-requested`. This component answers it:
 * - a remembered preference (`closeAction` app flag) acts immediately;
 * - otherwise it shows this dialog and, when "don't ask again" is ticked,
 *   persists the choice (Dexie + localStorage mirror).
 *
 * The Rust side hides the window after 1.5 s if nobody acknowledges (the
 * webview may still be booting), so the listener acknowledges FIRST — before
 * opening the dialog — and the fallback never races the prompt.
 *
 * @module components/CloseToTrayDialog
 */
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { isTauri } from '~/composables/bridge/transport';
import { getAppFlag, setAppFlag } from '~/composables/use-app-flags';
import { logUtil } from '~/utils/log';

/** Remembered close behaviour ('ask' or unset = prompt every time). */
const CLOSE_ACTION_FLAG = 'closeAction';

const { t } = useI18n();
const visible = ref(false);
const remember = ref(false);
let unlisten: (() => void) | null = null;

/** Hide the window; the tray icon / Alt+Space brings it back. */
async function hideToTray(): Promise<void> {
  const { getCurrentWindow } = await import('@tauri-apps/api/window');
  await getCurrentWindow().hide();
}

/** Quit for real: the Rust command sets the explicit-quit flag first. */
async function quitApp(): Promise<void> {
  const { invoke } = await import('@tauri-apps/api/core');
  await invoke('quit_app');
}

/** Apply one choice, persisting it when "don't ask again" is ticked. */
async function choose(choice: 'tray' | 'exit'): Promise<void> {
  visible.value = false;
  try {
    if (remember.value) await setAppFlag(CLOSE_ACTION_FLAG, choice);
    remember.value = false;
    if (choice === 'tray') await hideToTray();
    else await quitApp();
  } catch (e) {
    // The window stays open; the next close asks again (or the Rust fallback
    // hides it). Never leave an unhandled rejection behind a dialog button.
    logUtil.e('[CloseToTrayDialog] Failed to apply the close choice:', e);
  }
}

/** The Rust shell asked what to do with the close request. */
async function onCloseRequested(): Promise<void> {
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    // Stop the fallback hide BEFORE awaiting storage: the dialog may stay open
    // for a while, and a 1.5 s timer must not conceal it.
    await invoke('close_ack');
    const pref = await getAppFlag<string>(CLOSE_ACTION_FLAG);
    if (pref === 'tray') {
      await hideToTray();
      return;
    }
    if (pref === 'exit') {
      await quitApp();
      return;
    }
    remember.value = false;
    visible.value = true;
  } catch (e) {
    // Leave the close to the Rust fallback (it hides the window).
    logUtil.e('[CloseToTrayDialog] Failed to handle the close request:', e);
  }
}

onMounted(async () => {
  if (!isTauri()) return;
  const { listen } = await import('@tauri-apps/api/event');
  unlisten = await listen('close-requested', () => {
    void onCloseRequested();
  });
});

onBeforeUnmount(() => {
  unlisten?.();
  unlisten = null;
});
</script>

<i18n lang="json">
{
  "zh": {
    "closeTray": {
      "title": "关闭应用",
      "message": "关闭窗口后应用会继续在后台运行，可以从托盘图标或快捷键恢复。要最小化到托盘，还是直接退出？",
      "remember": "不再询问",
      "minimize": "最小化到托盘",
      "quit": "直接退出"
    }
  },
  "en": {
    "closeTray": {
      "title": "Close the app",
      "message": "Closing the window keeps the app running in the background; the tray icon or the shortcut brings it back. Minimize to the tray, or quit for real?",
      "remember": "Don't ask again",
      "minimize": "Minimize to tray",
      "quit": "Quit"
    }
  },
  "ja": {
    "closeTray": {
      "title": "アプリを閉じる",
      "message": "ウィンドウを閉じてもアプリはバックグラウンドで動作し続け、トレイアイコンかショートカットで復元できます。トレイに最小化しますか、それとも終了しますか？",
      "remember": "次回から確認しない",
      "minimize": "トレイに最小化",
      "quit": "終了する"
    }
  },
  "ko": {
    "closeTray": {
      "title": "앱 닫기",
      "message": "창을 닫아도 앱은 백그라운드에서 계속 실행되며, 트레이 아이콘이나 단축키로 복원할 수 있습니다. 트레이로 최소화할까요, 아니면 종료할까요?",
      "remember": "다시 묻지 않기",
      "minimize": "트레이로 최소화",
      "quit": "종료"
    }
  }
}
</i18n>
