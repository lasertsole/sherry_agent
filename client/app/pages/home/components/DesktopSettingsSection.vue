<template>
  <div class="flex flex-col gap-4" data-test="desktop-settings">
    <section
      class="flex flex-col gap-2 rounded-lg border border-solid border-gray-200 p-4 dark:border-gray-700">
      <label class="text-sm font-medium text-[#111827] dark:text-[#E5E7EB]">
        {{ t('config.desktop.closeBehavior') }}
      </label>
      <Select
        v-model="choice"
        :options="options"
        option-label="label"
        option-value="value"
        :disabled="!isDesktop"
        class="w-64 text-sm"
        data-test="desktop-close-behavior"
        @change="save" />
      <p class="m-0 text-xs text-gray-400">
        {{ t('config.desktop.closeBehaviorHint') }}
      </p>
      <p
        v-if="!isDesktop"
        class="m-0 text-xs text-amber-500">
        {{ t('config.desktop.desktopOnly') }}
      </p>
    </section>
  </div>
</template>

<script lang="ts" setup>
/**
 * Desktop-only preferences (System Config → Desktop).
 *
 * The close behaviour is an APP FLAG (Dexie + localStorage mirror), not a
 * backend setting: the window close is decided by the shell, and the value must
 * be readable before the backend is reachable. The same flag drives
 * `CloseToTrayDialog`, so this picker and the dialog never disagree.
 *
 * @module components/DesktopSettingsSection
 */
import { computed, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { isTauri } from '~/composables/bridge/transport';
import { getAppFlag, setAppFlag } from '~/composables/use-app-flags';

const { t } = useI18n();

const CLOSE_ACTION_FLAG = 'closeAction';
const isDesktop = isTauri();
const choice = ref<'ask' | 'tray' | 'exit'>('ask');

const options = computed(() => [
  { value: 'ask', label: t('config.desktop.closeAsk') },
  { value: 'tray', label: t('config.desktop.closeTray') },
  { value: 'exit', label: t('config.desktop.closeExit') }
]);

onMounted(async () => {
  const stored = await getAppFlag<string>(CLOSE_ACTION_FLAG);
  choice.value = stored === 'tray' || stored === 'exit' ? stored : 'ask';
});

/** Persist immediately (this section has no footer save button). */
async function save(): Promise<void> {
  await setAppFlag(CLOSE_ACTION_FLAG, choice.value);
}
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "desktop": {
        "closeBehavior": "关闭窗口时",
        "closeBehaviorHint": "点击窗口关闭按钮（×）时的行为；选“最小化到托盘”后可从托盘图标或 Alt+Space 恢复。",
        "closeAsk": "每次询问",
        "closeTray": "最小化到托盘",
        "closeExit": "直接退出",
        "desktopOnly": "此项仅在桌面版中生效。"
      }
    }
  },
  "en": {
    "config": {
      "desktop": {
        "closeBehavior": "When closing the window",
        "closeBehaviorHint": "What the window's close button (×) does. Minimize-to-tray can be restored from the tray icon or Alt+Space.",
        "closeAsk": "Ask every time",
        "closeTray": "Minimize to tray",
        "closeExit": "Quit",
        "desktopOnly": "This setting only applies to the desktop app."
      }
    }
  },
  "ja": {
    "config": {
      "desktop": {
        "closeBehavior": "ウィンドウを閉じるとき",
        "closeBehaviorHint": "ウィンドウの閉じるボタン（×）の動作です。「トレイに最小化」はトレイアイコンまたは Alt+Space で復元できます。",
        "closeAsk": "毎回確認する",
        "closeTray": "トレイに最小化",
        "closeExit": "終了する",
        "desktopOnly": "この設定はデスクトップ版でのみ有効です。"
      }
    }
  },
  "ko": {
    "config": {
      "desktop": {
        "closeBehavior": "창을 닫을 때",
        "closeBehaviorHint": "창 닫기 버튼(×)의 동작입니다. '트레이로 최소화'는 트레이 아이콘이나 Alt+Space로 복원할 수 있습니다.",
        "closeAsk": "매번 묻기",
        "closeTray": "트레이로 최소화",
        "closeExit": "종료",
        "desktopOnly": "이 설정은 데스크톱 앱에서만 적용됩니다."
      }
    }
  }
}
</i18n>
