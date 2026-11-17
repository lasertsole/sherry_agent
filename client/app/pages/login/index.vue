<template>
  <div class="flex min-h-screen w-full items-center justify-center bg-gray-50 px-4 text-theme-main dark:bg-[#141618]">
    <div
      class="w-full max-w-sm rounded-xl border border-solid border-gray-200 bg-white p-6 shadow-sm dark:border-gray-700 dark:bg-[#1a1d21]">
      <div class="mb-6 flex flex-col items-center gap-1">
        <span class="text-2xl">🍊{{ t('chatBox.defaultAiName') }}</span>
        <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('auth.login.subtitle') }}</span>
      </div>

      <form
        class="flex flex-col gap-3"
        v-debounce:submit.500="submit"
        @submit.prevent>
        <div
          v-if="auth.usernameRequired"
          class="flex flex-col gap-1">
          <label
            class="text-xs text-gray-500 dark:text-gray-400"
            for="auth-username"
            >{{ t('auth.login.username') }}</label
          >
          <InputText
            id="auth-username"
            v-model="username"
            data-test="login-username"
            autocomplete="username"
            :disabled="auth.busy"
            class="w-full"
            @update:model-value="auth.clearError()" />
        </div>
        <div class="flex flex-col gap-1">
          <label
            class="text-xs text-gray-500 dark:text-gray-400"
            for="auth-password"
            >{{ t('auth.login.password') }}</label
          >
          <Password
            input-id="auth-password"
            v-model="password"
            data-test="login-password"
            :feedback="false"
            toggle-mask
            autocomplete="current-password"
            :disabled="auth.busy"
            class="w-full"
            input-class="w-full"
            @update:model-value="auth.clearError()" />
        </div>

        <p
          v-if="auth.errorMessage"
          class="m-0 break-all text-xs text-red-500 dark:text-red-400"
          data-test="login-error">
          {{ errorText }}
        </p>

        <Button
          type="submit"
          :label="t('auth.login.submit')"
          icon="pi pi-sign-in"
          :loading="auth.busy"
          :disabled="!canSubmit || auth.busy"
          data-test="login-submit"
          class="mt-1 w-full" />
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useI18n } from 'vue-i18n';

const { t } = useI18n();
const auth = useAuthStore();
const route = useRoute();

const password = ref('');
const username = ref('');

/** A multi-tenant Ingress needs both fields; the single-account cell needs one. */
const canSubmit = computed(() => !!password.value && (!auth.usernameRequired || !!username.value.trim()));

/**
 * The line under the form: the throttle's own message (it names the wait),
 * otherwise the uniform "wrong password" text — a wrong password and a
 * throttled client must not be told apart by anything else.
 */
const errorText = computed(() => {
  if (auth.errorCode === 'too_many_attempts' && auth.errorMessage) return auth.errorMessage;
  if (auth.errorCode === 'no_tenant') return t('auth.login.noTenant');
  return t(auth.usernameRequired ? 'auth.login.failedMulti' : 'auth.login.failed');
});

/** Sign in; on success the guard's /home target is where the user was headed. */
async function submit(): Promise<void> {
  if (!password.value) return;
  const ok = await auth.login(password.value, auth.usernameRequired ? username.value.trim() : undefined);
  if (!ok) return;
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/home';
  await navigateTo(redirect);
}
</script>

<i18n lang="json">
{
  "zh": {
    "auth": {
      "login": {
        "failed": "密码错误（多次失败会临时锁定）",
        "failedMulti": "密码或用户名错误（多次失败会临时锁定）",
        "password": "密码",
        "username": "用户名",
        "noTenant": "该账号尚未分配 Cell，请联系管理员",
        "submit": "登 录",
        "subtitle": "请输入密码以继续"
      }
    }
  },
  "en": {
    "auth": {
      "login": {
        "failed": "Wrong password — repeated failures lock this client out for a while",
        "failedMulti": "Wrong password or username — repeated failures lock this client out for a while",
        "password": "Password",
        "username": "Username",
        "noTenant": "This account has no cell assigned; contact the administrator",
        "submit": "Sign in",
        "subtitle": "Enter the password to continue"
      }
    }
  },
  "ja": {
    "auth": {
      "login": {
        "failed": "パスワードが違います（失敗が続くと一時的にロックされます）",
        "failedMulti": "パスワードまたはユーザー名が違います（失敗が続くと一時的にロックされます）",
        "password": "パスワード",
        "username": "ユーザー名",
        "noTenant": "このアカウントにはセルが割り当てられていません。管理者に連絡してください",
        "submit": "ログイン",
        "subtitle": "続けるにはパスワードを入力してください"
      }
    }
  },
  "ko": {
    "auth": {
      "login": {
        "failed": "비밀번호가 올바르지 않습니다 (반복 실패 시 일시적으로 잠깁니다)",
        "failedMulti": "비밀번호 또는 사용자 이름이 올바르지 않습니다 (반복 실패 시 일시적으로 잠깁니다)",
        "password": "비밀번호",
        "username": "사용자 이름",
        "noTenant": "이 계정에 할당된 셀이 없습니다. 관리자에게 문의하십시오",
        "submit": "로그인",
        "subtitle": "계속하려면 비밀번호를 입력하세요"
      }
    }
  }
}
</i18n>
