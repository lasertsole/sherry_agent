import { describe, it, expect } from 'vitest';
import { useChatCardExpansion } from '../use-chat-card-expansion';

describe('useChatCardExpansion', () => {
  it('toggles tool-card expansion per message id', () => {
    const { expandedToolCards, toggleToolCard } = useChatCardExpansion();
    expect(expandedToolCards.has(1)).toBe(false);
    toggleToolCard(1);
    expect(expandedToolCards.has(1)).toBe(true);
    toggleToolCard(1);
    expect(expandedToolCards.has(1)).toBe(false);
  });

  it('toggles thinking expansion independently from tool cards', () => {
    const { expandedToolCards, expandedThinking, toggleThinking } = useChatCardExpansion();
    toggleThinking(5);
    expect(expandedThinking.has(5)).toBe(true);
    expect(expandedToolCards.has(5)).toBe(false);
    toggleThinking(5);
    expect(expandedThinking.has(5)).toBe(false);
  });

  it('keeps separate state per composable instance', () => {
    const a = useChatCardExpansion();
    const b = useChatCardExpansion();
    a.toggleToolCard(1);
    expect(a.expandedToolCards.has(1)).toBe(true);
    expect(b.expandedToolCards.has(1)).toBe(false);
  });
});
