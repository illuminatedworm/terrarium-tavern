import { describe, expect, it } from 'vitest';
import { archiveSourceText } from '../../web/lib/sceneNotes';

describe('scene notes', () => {
  it('matches source excerpts after identity and historical variable substitution', () => {
    expect(
      archiveSourceText(
        { raw: '{{user}}遇见{{char}}，在{{getvar::place}}。', snapshot: { state: { place: '旧港' } } },
        '小舟',
        '洛伊',
      ),
    ).toBe('小舟遇见洛伊，在旧港。');
  });
});
