import { vDebounce } from '~/directives/debounce';
import { vSafeHtml } from '~/directives/safeHtml';

export default defineNuxtPlugin(nuxtApp => {
  nuxtApp.vueApp.directive('debounce', vDebounce);
  nuxtApp.vueApp.directive('safe-html', vSafeHtml);
});
