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
          {{ t('auth.login.failed') }}
        </p>

        <Button
          type="submit"
          :label="t('auth.login.submit')"
          icon="pi pi-sign-in"
          :loading="auth.busy"
          :disabled="!username.trim() || !password || auth.busy"
          data-test="login-submit"
          class="mt-1 w-full" />
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { useI18n } from 'vue-i18n';

const { t } = useI18n();
const auth = useAuthStore();
const route = useRoute();

const username = ref('');
const password = ref('');

/** Sign in; on success the guard's /home target is where the user was headed. */
async function submit(): Promise<void> {
  if (!username.value.trim() || !password.value) return;
  const ok = await auth.login(username.value.trim(), password.value);
  if (!ok) return;
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/home';
  await navigateTo(redirect);
}
</script>

<i18n lang="json">
{
  "en": {
    "auth": {
      "login": {
        "failed": "Wrong username or password",
        "password": "Password",
        "submit": "Sign in",
        "subtitle": "Sign in to continue",
        "username": "Username"
      }
    }
  },
  "zh": {
    "auth": {
      "login": {
        "failed": "用户名或密码错误",
        "password": "密码",
        "submit": "登 录",
        "subtitle": "请登录以继续",
        "username": "用户名"
      }
    }
  },
  "ja": {
    "auth": {
      "login": {
        "failed": "ユーザー名またはパスワードが違います",
        "password": "パスワード",
        "submit": "ログイン",
        "subtitle": "続けるにはログインしてください",
        "username": "ユーザー名"
      }
    }
  },
  "ko": {
    "auth": {
      "login": {
        "failed": "사용자 이름 또는 비밀번호가 올바르지 않습니다",
        "password": "비밀번호",
        "submit": "로그인",
        "subtitle": "계속하려면 로그인하세요",
        "username": "사용자 이름"
      }
    }
  }
}
</i18n>
