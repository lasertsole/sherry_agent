/**
 * Vitest stub for Nuxt's virtual `#components` module.
 *
 * Bare Vitest has no Nuxt build step, so the auto-registered-component module
 * does not resolve. Export inert placeholders for the components imported via
 * `#components` in SFCs mounted by the integration suite.
 */
import { defineComponent, h } from 'vue';

export const ChatInputBox = defineComponent({
  name: 'ChatInputBox',
  render: () => h('div', { class: 'chat-input-box-stub' })
});
