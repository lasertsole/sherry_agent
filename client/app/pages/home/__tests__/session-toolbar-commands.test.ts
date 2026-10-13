import { describe, it, expect, vi } from 'vitest';
import { tools } from '../config';
import { buildSessionToolbarCommands, SESSION_TOOLBAR_EVENTS } from '../session-toolbar';

function build() {
  const handlers = {
    createSession: vi.fn(),
    uploadImage: vi.fn(),
    uploadAudio: vi.fn(),
    uploadVideo: vi.fn()
  };
  return { handlers, commands: buildSessionToolbarCommands(handlers) };
}

describe('buildSessionToolbarCommands', () => {
  it.each([...SESSION_TOOLBAR_EVENTS])('routes %s to its own handler only', event => {
    const { handlers, commands } = build();
    commands[event]?.();
    expect(handlers[event]).toHaveBeenCalledTimes(1);
    for (const other of SESSION_TOOLBAR_EVENTS) {
      if (other !== event) expect(handlers[other]).not.toHaveBeenCalled();
    }
  });

  it('registers exactly the toolbar event vocabulary', () => {
    const { commands } = build();
    expect(Object.keys(commands).sort()).toEqual([...SESSION_TOOLBAR_EVENTS].sort());
  });

  it('covers every session toolbar tool event', () => {
    const { commands } = build();
    for (const tool of tools) expect(commands).toHaveProperty(tool.event);
  });

  it('is a no-op for an unknown event', () => {
    const { handlers, commands } = build();
    expect(commands['does-not-exist']).toBeUndefined();
    commands['does-not-exist']?.();
    for (const event of SESSION_TOOLBAR_EVENTS) expect(handlers[event]).not.toHaveBeenCalled();
  });
});
