import { describe, expect, it } from 'vitest';
import { classifyFileFormat } from '../file-format';

/** The classifier is the file viewer's single branch decision point. */
describe('classifyFileFormat', () => {
  it.each([
    ['README.md', 'markdown'],
    ['guide.Markdown', 'markdown'],
    ['notes.markdown', 'markdown']
  ])('%s → markdown', (path, expected) => {
    expect(classifyFileFormat(path)).toBe(expected);
  });

  it.each([
    ['manual.PDF', 'pdf'],
    ['spec.pdf', 'pdf']
  ])('%s → pdf', (path, expected) => {
    expect(classifyFileFormat(path)).toBe(expected);
  });

  it.each([
    ['report.docx', 'docx'],
    ['Report.DOCX', 'docx']
  ])('%s → docx', (path, expected) => {
    expect(classifyFileFormat(path)).toBe(expected);
  });

  it.each([
    ['logo.png', 'image'],
    ['photo.JPG', 'image'],
    ['icon.svg', 'image'],
    ['anim.webp', 'image']
  ])('%s → image', (path, expected) => {
    expect(classifyFileFormat(path)).toBe(expected);
  });

  it.each([
    ['main.py', 'text'],
    ['no-extension', 'text'],
    ['legacy.doc', 'text'],
    ['archive.xlsx', 'text'],
    ['slides.pptx', 'text'],
    ['data.json', 'text']
  ])('%s → text fallback (F4)', (path, expected) => {
    expect(classifyFileFormat(path)).toBe(expected);
  });
});
