import { afterEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { ref } from 'vue';

/**
 * The first-run progress card. The real state machine is covered by
 * `use-backend-download.test.ts`; here the overlay is mounted against a
 * controlled state to pin what the user actually sees — nothing while idle, a
 * progress bar while downloading, the failure card with its retry when the
 * download breaks, and the hand-off line before the SPA re-boots.
 */
const state = ref<'idle' | 'running' | 'ready' | 'failed'>('idle');
const percent = ref(0);
const downloadedBytes = ref(0);
const totalBytes = ref(0);
const error = ref('');
const retry = vi.fn();
const initBackendDownload = vi.fn().mockResolvedValue(undefined);

vi.mock('~/composables/use-backend-download', () => ({
  useBackendDownload: () => ({
    state,
    percent,
    downloadedBytes,
    totalBytes,
    error,
    retry
  }),
  initBackendDownload
}));

// Imported AFTER the mock's backing state exists: `vi.mock` is hoisted, but the
// factory must not read module-scope bindings while they are still in their
// temporal dead zone, which a static import of the SFC would do.
const BackendDownloadOverlay = (await import('../BackendDownloadOverlay.vue')).default;

let mounted: ReturnType<typeof mount> | null = null;

function mountOverlay() {
  mounted = mount(BackendDownloadOverlay, { attachTo: document.body });
  return mounted;
}

// The card is teleported to <body>, so a failed assertion would otherwise leave
// its DOM behind for the next mount to find.
afterEach(() => {
  mounted?.unmount();
  mounted = null;
  document.body.innerHTML = '';
});

function reset(
  over: {
    state?: 'idle' | 'running' | 'ready' | 'failed';
    percent?: number;
    downloaded?: number;
    total?: number;
    error?: string;
  } = {}
) {
  state.value = over.state ?? 'idle';
  percent.value = over.percent ?? 0;
  downloadedBytes.value = over.downloaded ?? 0;
  totalBytes.value = over.total ?? 0;
  error.value = over.error ?? '';
}

describe('BackendDownloadOverlay.vue', () => {
  it('renders nothing while idle (a checkout run, or a backend already installed)', () => {
    reset();
    const wrapper = mountOverlay();

    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
  });

  it('shows the progress bar and the transferred volume while downloading', () => {
    const mb = 1024 * 1024;
    reset({ state: 'running', percent: 25, downloaded: 256 * mb, total: 1024 * mb });
    mountOverlay();

    // Teleported to <body>: query the real DOM, not the component root.
    const card = document.body.querySelector('[role="dialog"]')!;
    expect(card).not.toBeNull();
    expect(card.querySelector('h2')?.textContent).toContain('正在下载 Sherry 后端');

    const bar = card.querySelector('[role="progressbar"]')!;
    expect(bar.getAttribute('aria-valuenow')).toBe('25');
    expect((bar.firstElementChild as HTMLElement).style.width).toBe('25%');
    expect(card.textContent).toContain('256 / 1024 MB (25%)');
  });

  it('offers the retry when the download failed, and reports it', async () => {
    reset({ state: 'failed', error: 'HTTP 404 for sherry-backend-linux-x86_64.tar.gz' });
    mountOverlay();
    const card = document.body.querySelector('[role="dialog"]')!;

    expect(card.querySelector('[data-test="backend-download-error"]')?.textContent).toContain('HTTP 404');
    const retryButton = card.querySelector('[data-test="backend-download-retry"]') as HTMLElement;
    expect(retryButton.textContent).toContain('重试下载');

    retryButton.click();
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it('hands off to the restart line once the backend is up', () => {
    reset({ state: 'ready', percent: 100, downloaded: 1024 * 1024, total: 1024 * 1024 });
    mountOverlay();
    const card = document.body.querySelector('[role="dialog"]')!;

    expect(card.textContent).toContain('后端已就绪，正在启动');
    expect(card.querySelector('[role="progressbar"]')).toBeNull();
    // The state is already 'ready' at mount, so the restart watcher must NOT
    // have armed a reload (the watchers only fire on a transition).
  });

  it("pulls the shell's state on mount", () => {
    reset();
    mountOverlay();

    expect(initBackendDownload).toHaveBeenCalled();
  });
});
