/**
 * The folded-turn header's image peek: a turn that generated pictures shows
 * thumbnails beside its summary trigger, so a picture hidden inside the folded
 * tool cards is still discoverable. Clicking a thumbnail opens the shared
 * gallery without toggling the fold; clicking the trigger toggles it.
 */
import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import ChatTurnHistoryStatus from '../ChatTurnHistoryStatus.vue';

const URI = 'sherry-artifact://s1/image-0123456789abcdef.png';
const URL = 'http://localhost:8080/artifacts/s1/image-0123456789abcdef.png';

const mountStatus = (props: Record<string, unknown> = {}) =>
  mount(ChatTurnHistoryStatus, {
    props: {
      label: '3 tools · 2s',
      expanded: false,
      expandLabel: 'expand',
      collapseLabel: 'collapse',
      ...props
    }
  });

describe('ChatTurnHistoryStatus', () => {
  it('renders the summary trigger without an image peek when nothing generated images', () => {
    const wrapper = mountStatus({ artifactUris: [] });

    expect(wrapper.find('[data-test="artifact-image-gallery"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="turn-history-status"]').attributes('aria-expanded')).toBe('false');
  });

  it('peeks every distinct artifact image in the folded tool results', () => {
    const wrapper = mountStatus({ artifactUris: [URI, URI] });

    expect(wrapper.findAll('[data-test="artifact-image-thumb"]')).toHaveLength(1);
    expect(wrapper.get('[data-test="artifact-image-thumb"] img').attributes('src')).toBe(URL);
  });

  it('caps the peek row and reports the remainder', () => {
    const uris = Array.from(
      { length: 6 },
      (_, index) => `sherry-artifact://s1/image-${index.toString(16).padStart(16, '0')}.png`
    );
    const wrapper = mountStatus({ artifactUris: uris });

    expect(wrapper.findAll('[data-test="artifact-image-thumb"]')).toHaveLength(4);
    expect(wrapper.text()).toContain('+2');
  });

  it('opens the gallery from a thumbnail without toggling the fold', async () => {
    const wrapper = mountStatus({ artifactUris: [URI] });
    const preview = await import('../../../composables/useImagePreview');
    const gallery = preview.useImagePreview();

    await wrapper.get('[data-test="artifact-image-thumb"]').trigger('click');

    expect(gallery.isPreviewVisible.value).toBe(true);
    expect(gallery.galleryItems.value).toEqual([URL]);
    expect(wrapper.emitted('toggle')).toBeUndefined();
    gallery.closePreview();
  });

  it('still toggles from the summary trigger', async () => {
    const wrapper = mountStatus({ artifactUris: [URI] });

    await wrapper.get('[data-test="turn-history-status"]').trigger('click');

    expect(wrapper.emitted('toggle')).toHaveLength(1);
  });
});
