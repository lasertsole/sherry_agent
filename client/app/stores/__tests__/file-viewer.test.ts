import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useFileViewerStore, MAX_BLOB_CACHE_BYTES_FOR_TEST } from '../file-viewer';

/**
 * The file-viewer cache: two entry shapes (text decode / raw blob bytes), the
 * 20-entry age cap both share, and the blob-only byte budget (binary previews
 * can be 20 MiB each — the count cap alone could pin hundreds of MB).
 */
describe('useFileViewerStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('caches text entries through the legacy call shape', () => {
    const store = useFileViewerStore();
    store.put('sid', 'README.md', '# Hi', 4);

    const entry = store.get('sid', 'README.md');
    expect(entry?.kind).toBe('text');
    if (entry?.kind === 'text') expect(entry.content).toBe('# Hi');
  });

  it('caches blob entries with mime and size', () => {
    const store = useFileViewerStore();
    const bytes = new ArrayBuffer(8);
    store.put('sid', 'doc.pdf', { kind: 'blob', bytes, mime: 'application/pdf', size: 8 });

    const entry = store.get('sid', 'doc.pdf');
    expect(entry?.kind).toBe('blob');
    if (entry?.kind === 'blob') {
      expect(entry.bytes).toBe(bytes); // identity: the cached buffer, not a copy
      expect(entry.mime).toBe('application/pdf');
    }
  });

  it('evicts the oldest entries beyond the 20-entry cap (either shape)', () => {
    const store = useFileViewerStore();
    for (let i = 0; i < 22; i++) {
      store.put('sid', `f${i}.txt`, `text-${i}`, 6);
    }
    expect(store.get('sid', 'f0.txt')).toBeNull();
    expect(store.get('sid', 'f21.txt')?.kind).toBe('text');

    const blob = new ArrayBuffer(4);
    store.put('sid', 'doc.pdf', { kind: 'blob', bytes: blob, mime: 'application/pdf', size: 4 });
    // The blob insert itself pushed the count over: the oldest text goes first.
    expect(store.get('sid', 'f1.txt')).toBeNull();
    expect(store.get('sid', 'doc.pdf')?.kind).toBe('blob');
  });

  it('evicts oldest blobs first when the byte budget overflows', () => {
    const store = useFileViewerStore();
    const big = MAX_BLOB_CACHE_BYTES_FOR_TEST / 2 + 1024;
    store.put('sid', 'a.pdf', { kind: 'blob', bytes: new ArrayBuffer(big), mime: 'application/pdf', size: big });
    store.put('sid', 'b.pdf', { kind: 'blob', bytes: new ArrayBuffer(big), mime: 'application/pdf', size: big });
    // a+b exceed the budget: the OLDEST binary (a) goes, the newest survives —
    // even though a text entry is older still (text weighs nothing).
    store.put('sid', 'old.txt', 'tiny', 4);

    expect(store.get('sid', 'a.pdf')).toBeNull();
    expect(store.get('sid', 'b.pdf')?.kind).toBe('blob');
    expect(store.get('sid', 'old.txt')?.kind).toBe('text');
  });

  it('clear() drops everything (project switch)', () => {
    const store = useFileViewerStore();
    store.put('sid', 'README.md', '# Hi', 4);
    store.put('sid', 'doc.pdf', { kind: 'blob', bytes: new ArrayBuffer(4), mime: 'application/pdf', size: 4 });

    store.clear();

    expect(store.get('sid', 'README.md')).toBeNull();
    expect(store.get('sid', 'doc.pdf')).toBeNull();
  });
});
