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

/** Turn protection on (account exists) or off — both need the password (R4). */
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
