import { describe, expect, it } from 'vitest';
import { gb, integer } from './contracts';
describe('exact accounting values', () => {
  it('retains byte precision beyond the JavaScript safe integer range', () => {
    expect(gb('9007199254740993')).toBe('9,007,199.254740 GB');
    expect(integer('9007199254740993')).toBe('9,007,199,254,740,993');
    expect(gb('999999999')).toBe('0.999999 GB');
  });
});
