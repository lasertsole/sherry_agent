import { computed } from 'vue';
import { useI18n } from 'vue-i18n';

/** The four shipped interface locales. */
export type LocaleCode = 'zh' | 'en' | 'ja' | 'ko';

/** Every selectable locale, each named in its own language. */
export const LOCALE_OPTIONS: ReadonlyArray<{ code: LocaleCode; name: string }> = [
  { code: 'zh', name: '简体中文' },
  { code: 'en', name: 'English' },
  { code: 'ja', name: '日本語' },
  { code: 'ko', name: '한국어' }
];

/**
 * nuxt-i18n's preference cookie (`detectBrowserLanguage: false`, so the module
 * never writes it itself). `app.vue` reads the SAME key on boot — it is the only
 * thing that makes the choice survive a reload — so every switch entry point
 * must write it.
 */
export const LOCALE_PREF_COOKIE = 'i18n_redirected';

/**
 * The one way to change the interface language.
 *
 * Two steps, both required: `setLocale` re-renders the app in place (under the
 * `no_prefix` strategy its internal `navigate()` returns early, so a `/home/:id`
 * URL stays stable), and the cookie write is what `app.vue` reads on the next
 * boot. Calling `setLocale` alone switches the current session and forgets the
 * choice on reload.
 */
export function useLocaleSwitch() {
  const { locale, setLocale } = useI18n();

  /** The active locale code. */
  const current = computed(() => String(locale.value) as LocaleCode);

  /**
   * Switch the interface language (no-op when it is already active).
   * @param code Target locale.
   */
  async function switchTo(code: LocaleCode): Promise<void> {
    if (code === current.value) return;
    await setLocale(code);
    const preference = useCookie(LOCALE_PREF_COOKIE);
    preference.value = code;
  }

  return { current, switchTo, options: LOCALE_OPTIONS };
}
