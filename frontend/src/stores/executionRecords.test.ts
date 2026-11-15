import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useExecutionRecords } from './executionRecords';

beforeEach(() => {
  setActivePinia(createPinia());
  vi.restoreAllMocks();
});

describe('execution records revisit cache', () => {
  it('restores the page and filters across page owners without mixing queries', () => {
    const first = useExecutionRecords();
    first.query = 'video';
    first.status = 'failed';
    first.cursor = 'page-two';
    first.history = [null];
    const value = { items: [], next_cursor: 'page-three' };
    first.remember(first.key(), value);
    const reopened = useExecutionRecords();
    expect(reopened.cached()).toBe(value);
    expect(reopened.cursor).toBe('page-two');
    expect(reopened.history).toEqual([null]);
    reopened.status = 'succeeded';
    expect(reopened.cached()).toBeUndefined();
    reopened.status = 'failed';
    reopened.query = 'different';
    expect(reopened.cached()).toBeUndefined();
  });
  it('expires old data and bounds memory while preserving the most recent pages', () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(1000);
    const state = useExecutionRecords();
    state.remember(state.key(), { items: [], next_cursor: null });
    now.mockReturnValue(122000);
    expect(state.cached()).toBeUndefined();
    for (let i = 0; i < 9; i++) {
      state.query = String(i);
      state.remember(state.key(), { items: [], next_cursor: String(i) });
    }
    expect(state.cached()?.next_cursor).toBe('8');
    state.query = '0';
    expect(state.cached()).toBeUndefined();
    state.query = '1';
    expect(state.cached()?.next_cursor).toBe('1');
  });
});
