<template>
  <!-- Recording timer next to the mic: tabular digits keep the mm:ss steady. -->
  <span
    v-if="recording"
    class="voiceTimer"
    >{{ timerLabel }}</span
  >
  <!-- Voice input: mic → record → transcribe → insert at the caret. Hidden while
      the box is disabled (a HITL card owns the input slot); disabled when STT
      is unconfigured (the status route's weights probe). While recording the
      button turns into the stop control, so it must stay clickable. -->
  <Button
    v-if="!disabled"
    v-debounce:click.300="toggleRecording"
    class="voice"
    :class="{ 'voice-recording': recording }"
    :icon="icon"
    text
    rounded
    :severity="recording ? 'danger' : 'secondary'"
    :aria-label="ariaLabel"
    :title="tooltip"
    :disabled="startBlocked && !recording" />
</template>

<script lang="ts" setup>
import { computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import type { VoiceErrorKind } from '~/composables/use-voice-input';

/**
 * The composer's voice-record control: mic → record → transcribe, with the
 * transcript handed to the composer through `transcript`. The recording state
 * machine itself is the UI-free `useVoiceInput`; this component owns the button
 * affordances and the user-facing failure notices.
 */
const props = defineProps<{
  /** Whether the composer forbids input (a HITL card occupies the slot). */
  disabled: boolean;
}>();

const emit = defineEmits<{
  /** A finished transcription, ready to insert at the caret. */
  (e: 'transcript', text: string): void;
}>();

const { t } = useI18n();

const voice = useVoiceInput();

const recording = computed(() => voice.state.value === 'recording');
const transcribing = computed(() => voice.state.value === 'transcribing');

/** The mic is blocked while a transcription runs or the box itself is disabled; unconfigured STT stays CLICKABLE — the click toasts the reason instead of silently refusing. */
const startBlocked = computed(() => props.disabled || transcribing.value);

const icon = computed(() =>
  transcribing.value ? 'pi pi-spin pi-spinner' : recording.value ? 'pi pi-stop-circle' : 'pi pi-microphone'
);

const timerLabel = computed(() => {
  const total = voice.elapsedSec.value;
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
});

const ariaLabel = computed(() => (recording.value ? t('chatInput.voiceStop') : t('chatInput.voiceRecord')));

const tooltip = computed(() => {
  if (voice.isSttAvailable.value === false) return t('chatInput.voiceUnavailable');
  return ariaLabel.value;
});

/** Toast-able failure kinds → the `chatInput.voice*` key that explains them. */
const VOICE_ERROR_KEYS: Record<VoiceErrorKind, string> = {
  permission: 'chatInput.voicePermissionDenied',
  unsupported: 'chatInput.voiceFailed',
  too_short: 'chatInput.voiceTooShort',
  warming: 'chatInput.voiceWarming',
  unavailable: 'chatInput.voiceUnavailable',
  failed: 'chatInput.voiceFailed'
};

/** Mic click: toggle record/stop; the stop half hands the transcript up. */
async function toggleRecording(): Promise<void> {
  if (recording.value) {
    const result = await voice.stopRecording();
    if (result.ok && result.text) {
      emit('transcript', result.text);
    } else if (result.reason && result.reason !== 'idle') {
      toastWarn(t(VOICE_ERROR_KEYS[result.reason as VoiceErrorKind] ?? 'chatInput.voiceFailed'));
    }
    return;
  }
  const started = await voice.startRecording();
  if (!started && voice.lastError.value) {
    toastWarn(t(VOICE_ERROR_KEYS[voice.lastError.value] ?? 'chatInput.voiceFailed'));
  }
}

onMounted(() => {
  // Voice availability is a one-shot probe; a backend that is down leaves
  // `null` (unknown) and the button stays clickable.
  void voice.probeAvailability();
});
</script>

<style lang="scss" scoped>
// Recording timer next to the mic button: tabular digits keep the mm:ss steady.
.voiceTimer {
  font-size: 0.75rem;
  color: #ef4444;
  font-variant-numeric: tabular-nums;
  user-select: none;
}

// The recording mic breathes instead of blinking — a stop control you can
// still aim at, never a seizure risk.
.voice.voice-recording {
  animation: voice-pulse 1.2s ease-in-out infinite;
}

@keyframes voice-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}
</style>
