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
        @submit.prevent="submit">
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
          :disabled="!password || auth.busy"
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

/**
 * The line under the form: the throttle's own message (it names the wait),
 * otherwise the uniform "wrong password" text — a wrong password and a
 * throttled client must not be told apart by anything else.
 */
const errorText = computed(() =>
  auth.errorCode === 'too_many_attempts' && auth.errorMessage ? auth.errorMessage : t('auth.login.failed')
);

/** Sign in; on success the guard's /home target is where the user was headed. */
async function submit(): Promise<void> {
  if (!password.value) return;
  const ok = await auth.login(password.value);
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
        "password": "密码",
        "submit": "登 录",
        "subtitle": "请输入密码以继续"
      }
    }
  },
  "en": {
    "auth": {
      "login": {
        "failed": "Wrong password — repeated failures lock this client out for a while",
        "password": "Password",
        "submit": "Sign in",
        "subtitle": "Enter the password to continue"
      }
    }
  },
  "ja": {
    "auth": {
      "login": {
        "failed": "パスワードが違います（失敗が続くと一時的にロックされます）",
        "password": "パスワード",
        "submit": "ログイン",
        "subtitle": "続けるにはパスワードを入力してください"
      }
    }
  },
  "ko": {
    "auth": {
      "login": {
        "failed": "비밀번호가 올바르지 않습니다 (반복 실패 시 일시적으로 잠깁니다)",
        "password": "비밀번호",
        "submit": "로그인",
        "subtitle": "계속하려면 비밀번호를 입력하세요"
      }
    }
  }
}
</i18n>
