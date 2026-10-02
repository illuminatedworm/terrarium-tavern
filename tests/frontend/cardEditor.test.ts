import { describe, expect, it } from 'vitest';
import { mentionCard } from '../../web/lib/cardEditor';

describe('explicit card references', () => {
  it('replaces only the active mention and preserves earlier prose', () => {
    expect(mentionCard('保留 @C1 的人设，参考 @', 'C2')).toBe('保留 @C1 的人设，参考 @C2 ');
    expect(mentionCard('根据这两张卡', 'C3')).toBe('根据这两张卡 @C3 ');
    expect(mentionCard('@书店', 'C1')).toBe('@C1 ');
  });
});
