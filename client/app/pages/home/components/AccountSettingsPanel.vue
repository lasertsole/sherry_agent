<template>
  <div class="flex h-full min-h-0 flex-col gap-3 overflow-auto p-1 text-sm">
    <!-- Status -->
    <div class="flex items-center gap-2">
      <i :class="auth.authEnabled ? 'pi pi-shield text-emerald-500' : 'pi pi-shield text-gray-400'"></i>
      <span data-test="auth-status">
        {{ auth.authEnabled ? t('auth.account.statusOn') : t('auth.account.statusOff') }}
      </span>
      <span
        v-if="auth.user"
        class="ml-auto text-xs text-gray-500 dark:text-gray-400"
        data-test="auth-current-user">
        {{ auth.user.username }}
      </span>
    </div>
    <p class="m-0 text-xs text-gray-500 dark:text-gray-400">
      {{ t('auth.account.scopeHint') }}
    </p>

    <!-- No account yet: first-time setup -->
    <template v-if="!auth.hasAccount">
      <p
        class="m-0 text-xs text-gray-500 dark:text-gray-400"
        data-test="auth-setup-hint">
        {{ t('auth.account.setupHint') }}
      </p>
      <div class="flex flex-col gap-2">
        <InputText
          v-model="setupUsername"
          :placeholder="t('auth.account.usernamePlaceholder')"
          data-test="auth-setup-username"
          autocomplete="username" />
        <Password
          v-model="setupPassword"
          :placeholder="t('auth.account.passwordPlaceholder')"
          data-test="auth-setup-password"
          :feedback="false"
          toggle-mask
          input-class="w-full"
          class="w-full"
          autocomplete="new-password" />
        <Password
          v-model="setupConfirm"
          :placeholder="t('auth.account.confirmPlaceholder')"
          data-test="auth-setup-confirm"
          :feedback="false"
          toggle-mask
          input-class="w-full"
          class="w-full"
          autocomplete="new-password" />
        <Button
          :label="t('auth.account.enable')"
          icon="pi pi-shield"
          :loading="auth.busy"
          :disabled="!canSetup"
          data-test="auth-setup-submit"
          @click="submitSetup" />
      </div>
    </template>

    <!-- Account exists: change it, or flip the switch -->
    <template v-else>
      <div
        v-if="auth.authEnabled"
        class="flex flex-col gap-2 border-t border-solid border-gray-100 pt-3 dark:border-gray-800">
        <p class="m-0 text-xs font-medium text-gray-500 dark:text-gray-400">
          {{ t('auth.account.changeHint') }}
        </p>
        <InputText
          v-model="newUsername"
          :placeholder="t('auth.account.newUsername')"
          data-test="auth-new-username" />
        <Password
          v-model="newPassword"
          :placeholder="t('auth.account.newPassword')"
          data-test="auth-new-password"
          :feedback="false"
          toggle-mask
          input-class="w-full"
          class="w-full"
          autocomplete="new-password" />
        <Password
          v-model="currentPassword"
          :placeholder="t('auth.account.currentPassword')"
          data-test="auth-current-password"
          :feedback="false"
          toggle-mask
          input-class="w-full"
          class="w-full"
          autocomplete="current-password" />
        <Button
          :label="t('auth.account.save')"
          icon="pi pi-check"
          :loading="auth.busy"
          :disabled="!canSaveChanges"
          data-test="auth-save"
          @click="submitChanges" />
      </div>

      <div class="flex flex-col gap-2 border-t border-solid border-gray-100 pt-3 dark:border-gray-800">
        <p class="m-0 text-xs text-gray-500 dark:text-gray-400">
          {{ auth.authEnabled ? t('auth.account.disableHint') : t('auth.account.enableHint') }}
        </p>
        <Password
          v-model="switchPassword"
          :placeholder="t('auth.account.currentPassword')"
          data-test="auth-switch-password"
          :feedback="false"
          toggle-mask
          input-class="w-full"
          class="w-full"
          autocomplete="current-password" />
        <Button
          :label="auth.authEnabled ? t('auth.account.disable') : t('auth.account.enable')"
          :icon="auth.authEnabled ? 'pi pi-lock-open' : 'pi pi-lock'"
          :severity="auth.authEnabled ? 'danger' : undefined"
          :loading="auth.busy"
          :disabled="!switchPassword"
          :data-test="auth.authEnabled ? 'auth-disable' : 'auth-enable'"
          @click="submitSwitch" />
      </div>
    </template>

    <p
      v-if="auth.errorMessage"
      class="m-0 break-all text-xs text-red-500 dark:text-red-400"
      data-test="auth-error">
      {{ auth.errorMessage }}
    </p>
    <p
      v-if="successMessage"
      class="m-0 text-xs text-emerald-600 dark:text-emerald-400"
      data-test="auth-success">
      {{ successMessage }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';

const { t } = useI18n();
const auth = useAuthStore();

const setupUsername = ref('');
const setupPassword = ref('');
const setupConfirm = ref('');
const newUsername = ref('');
const newPassword = ref('');
const currentPassword = ref('');
const switchPassword = ref('');
const successMessage = ref<string | null>(null);

const canSetup = computed(
  () => !!setupUsername.value.trim() && !!setupPassword.value && setupPassword.value === setupConfirm.value
);
const canSaveChanges = computed(
  () =>
    !!currentPassword.value &&
    (!!newUsername.value.trim() || !!newPassword.value) &&
    (!newPassword.value || newPassword.value.length >= 8)
);

/** Refresh the flags when the panel opens (the switch may have moved elsewhere). */
onMounted(async () => {
  await auth.checkAuthStatus(true);
  await auth.fetchUser();
});

/** First-time setup: creates the account and enables protection. */
async function submitSetup(): Promise<void> {
  successMessage.value = null;
  if (!canSetup.value) {
    auth.clearError();
    return;
  }
  if (setupPassword.value !== setupConfirm.value) return;
  const ok = await auth.setupAccount(setupUsername.value.trim(), setupPassword.value);
  if (ok) {
    successMessage.value = t('auth.account.enabled');
    setupPassword.value = '';
    setupConfirm.value = '';
  }
}

/** Change the username and/or password (the current password is mandatory). */
async function submitChanges(): Promise<void> {
  successMessage.value = null;
  const ok = await auth.updateAccount(
    currentPassword.value,
    newUsername.value.trim() || undefined,
    newPassword.value || undefined
  );
  if (!ok) return;
  successMessage.value = t('auth.account.saved');
  newUsername.value = '';
  newPassword.value = '';
  currentPassword.value = '';
}

/** Turn protection on (account exists) or off — both need the password. */
async function submitSwitch(): Promise<void> {
  successMessage.value = null;
  const ok = auth.authEnabled
    ? await auth.disableAuth(switchPassword.value)
    : await auth.enableAuth(switchPassword.value);
  if (ok) {
    successMessage.value = auth.authEnabled ? t('auth.account.enabled') : t('auth.account.disabled');
  }
  switchPassword.value = '';
}

/** Password confirmation mismatch is shown by disabling the button (see canSetup). */
</script>

<i18n lang="json">
{
  "en": {
    "auth": {
      "account": {
        "changeHint": "Change the username or password (current password required)",
        "confirmPlaceholder": "Confirm password",
        "currentPassword": "Current password",
        "disable": "Disable login protection",
        "disableHint": "Turning protection off requires the current password.",
        "disabled": "Login protection disabled",
        "enable": "Enable login protection",
        "enableHint": "Turning protection back on requires the current password.",
        "enabled": "Login protection enabled",
        "newPassword": "New password (optional, min 8 characters)",
        "newUsername": "New username (optional)",
        "passwordPlaceholder": "Password (min 8 characters)",
        "save": "Save",
        "saved": "Saved",
        "scopeHint": "Once enabled, access from other machines needs a login; this machine never does.",
        "setupHint": "Set a username and password to require a login from other machines.",
        "statusOff": "Login protection is off",
        "statusOn": "Login protection is on",
        "usernamePlaceholder": "Username"
      }
    }
  },
  "zh": {
    "auth": {
      "account": {
        "changeHint": "修改用户名或密码（需输入当前密码）",
        "confirmPlaceholder": "确认密码",
        "currentPassword": "当前密码",
        "disable": "关闭登录保护",
        "disableHint": "关闭登录保护需要输入当前密码确认。",
        "disabled": "登录保护已关闭",
        "enable": "启用登录保护",
        "enableHint": "重新启用登录保护需要输入当前密码确认。",
        "enabled": "登录保护已启用",
        "newPassword": "新密码（可选，至少 8 位）",
        "newUsername": "新用户名（可选）",
        "passwordPlaceholder": "密码（至少 8 位）",
        "save": "保存",
        "saved": "已保存",
        "scopeHint": "启用后，来自非本机的访问需要登录；本机访问始终免登录。",
        "setupHint": "设置用户名与密码后，非本机访问将需要登录。",
        "statusOff": "登录保护未启用",
        "statusOn": "登录保护已启用",
        "usernamePlaceholder": "用户名"
      }
    }
  },
  "ja": {
    "auth": {
      "account": {
        "changeHint": "ユーザー名／パスワードの変更（現在のパスワードが必要）",
        "confirmPlaceholder": "パスワードの確認",
        "currentPassword": "現在のパスワード",
        "disable": "ログイン保護を解除する",
        "disableHint": "保護を解除するには現在のパスワードが必要です。",
        "disabled": "ログイン保護を解除しました",
        "enable": "ログイン保護を有効にする",
        "enableHint": "保護を再度有効にするには現在のパスワードが必要です。",
        "enabled": "ログイン保護を有効にしました",
        "newPassword": "新しいパスワード（任意・8文字以上）",
        "newUsername": "新しいユーザー名（任意）",
        "passwordPlaceholder": "パスワード（8文字以上）",
        "save": "保存",
        "saved": "保存しました",
        "scopeHint": "有効にすると、他の端末からのアクセスにログインが必要になります（この端末は不要）。",
        "setupHint": "ユーザー名とパスワードを設定すると、他の端末からのアクセスにログインが必要になります。",
        "statusOff": "ログイン保護は無効です",
        "statusOn": "ログイン保護は有効です",
        "usernamePlaceholder": "ユーザー名"
      }
    }
  },
  "ko": {
    "auth": {
      "account": {
        "changeHint": "사용자 이름/비밀번호 변경 (현재 비밀번호 필요)",
        "confirmPlaceholder": "비밀번호 확인",
        "currentPassword": "현재 비밀번호",
        "disable": "로그인 보호 끄기",
        "disableHint": "보호를 끄려면 현재 비밀번호가 필요합니다.",
        "disabled": "로그인 보호를 껐습니다",
        "enable": "로그인 보호 켜기",
        "enableHint": "보호를 다시 켜려면 현재 비밀번호가 필요합니다.",
        "enabled": "로그인 보호를 켰습니다",
        "newPassword": "새 비밀번호 (선택, 8자 이상)",
        "newUsername": "새 사용자 이름 (선택)",
        "passwordPlaceholder": "비밀번호 (8자 이상)",
        "save": "저장",
        "saved": "저장했습니다",
        "scopeHint": "켜면 다른 기기에서의 접속에 로그인이 필요합니다. 이 기기는 항상 면제됩니다.",
        "setupHint": "사용자 이름과 비밀번호를 설정하면 다른 기기에서 로그인이 필요합니다.",
        "statusOff": "로그인 보호가 꺼져 있습니다",
        "statusOn": "로그인 보호가 켜져 있습니다",
        "usernamePlaceholder": "사용자 이름"
      }
    }
  }
}
</i18n>
