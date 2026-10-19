import { describe, expect, it } from 'vitest';
import { clone, difference, rebaseSettings, type SettingsDocument } from '../../web/lib/settingsDocument';

const fixture = () =>
  ({
    version: 5,
    full_agent: { model: { model: 'full', reply_tokens: 2048 }, system_prompt: 'full prompt' },
    half_agent: { foreground: { model: 'writer' }, background: { model: 'tracker' } },
    shared: { providers: [{ id: 'p', name: 'Provider', has_api_key: true }] },
    revisions: { full_agent: 0, half_agent: 0, shared: 0 },
    pages: [],
    prompt_definitions: [],
  }) as unknown as SettingsDocument;

describe('settings acknowledgements', () => {
  it('keeps edits made while a save is in flight and edits in other scopes', () => {
    const saved = fixture();
    const draft = clone(saved);
    draft.full_agent.model.reply_tokens = 4096;
    draft.half_agent.background.model = 'new tracker';
    const response = clone(saved);
    response.full_agent.model.reply_tokens = 3000;
    response.revisions.full_agent = 1;
    const rebased = rebaseSettings(saved, draft, response, 'full_agent', { 'model.reply_tokens': 3000 });
    expect(rebased.full_agent.model.reply_tokens).toBe(4096);
    expect(rebased.half_agent.background.model).toBe('new tracker');
    expect(rebased.revisions.full_agent).toBe(1);
  });
  it('does not repeatedly send an acknowledged secret omitted from the response', () => {
    const saved = fixture();
    const draft = clone(saved);
    draft.shared.providers[0].api_key = 'test-secret';
    const rebased = rebaseSettings(saved, draft, clone(saved), 'shared', {
      providers: draft.shared.providers,
    });
    expect(rebased.shared.providers[0].api_key).toBeUndefined();
    expect(difference(saved.shared, rebased.shared)).toEqual({});
  });
  it('keeps a newer secret typed before the older request completes', () => {
    const saved = fixture();
    const draft = clone(saved);
    const sent = clone(saved.shared.providers);
    sent[0].api_key = 'first';
    draft.shared.providers[0].api_key = 'second';
    const rebased = rebaseSettings(saved, draft, clone(saved), 'shared', { providers: sent });
    expect(rebased.shared.providers[0].api_key).toBe('second');
  });
  it('acknowledges a shared search key leaf without retaining or resending it', () => {
    const saved = fixture();
    saved.shared.web = {
      exa: { enabled: true, auth_mode: 'api_key', has_api_key: true },
    } as SettingsDocument['shared']['web'];
    const draft = clone(saved);
    draft.shared.web.exa.api_key = 'search-secret';
    const rebased = rebaseSettings(saved, draft, clone(saved), 'shared', {
      'web.exa.api_key': 'search-secret',
    });
    expect(rebased.shared.web.exa.api_key).toBeUndefined();
    expect(difference(saved.shared, rebased.shared)).toEqual({});
    draft.shared.web.exa.api_key = '';
    const cleared = clone(saved);
    cleared.shared.web.exa.has_api_key = false;
    expect(rebaseSettings(saved, draft, cleared, 'shared', { 'web.exa.api_key': '' }).shared.web.exa).toEqual(
      cleared.shared.web.exa,
    );
  });
  it('uses leaf paths while preserving null and atomic ordered arrays', () => {
    expect(
      difference(
        { model: { temperature: 0.8 }, blocks: ['a', 'b'] },
        { model: { temperature: null }, blocks: ['b', 'a'] },
      ),
    ).toEqual({ 'model.temperature': null, blocks: ['b', 'a'] });
  });
  it('creates nullable channel profiles atomically and edits existing profiles by leaf', () => {
    const profile = { model: 'official', reply_tokens: 2048 };
    expect(difference({ official_model: null }, { official_model: profile })).toEqual({
      official_model: profile,
    });
    expect(
      difference({ advisor: { official_model: null } }, { advisor: { official_model: profile } }),
    ).toEqual({ 'advisor.official_model': profile });
    expect(
      difference({ official_model: profile }, { official_model: { ...profile, reply_tokens: 4096 } }),
    ).toEqual({ 'official_model.reply_tokens': 4096 });
    expect(difference({ official_model: profile }, { official_model: null })).toEqual({
      official_model: null,
    });
  });
  it('keeps edits made while a nullable channel profile is being created', () => {
    const saved = fixture();
    saved.full_agent.official_model = null;
    const draft = clone(saved);
    draft.full_agent.official_model = { ...draft.full_agent.model, model: 'official' };
    const sent = difference(saved.full_agent, draft.full_agent);
    const response = clone(draft);
    response.revisions.full_agent = 1;
    draft.full_agent.official_model.reply_tokens = 4096;
    const rebased = rebaseSettings(saved, draft, response, 'full_agent', sent);
    expect(rebased.full_agent.official_model).toEqual(draft.full_agent.official_model);
    expect(rebased.full_agent.model).toEqual(saved.full_agent.model);
    expect(difference(response.full_agent, rebased.full_agent)).toEqual({
      'official_model.reply_tokens': 4096,
    });
  });
  it('accepts server updates to an untouched mode without copying the edited mode into it', () => {
    const saved = fixture();
    const draft = clone(saved);
    draft.full_agent.model.model = 'new full';
    const response = clone(draft);
    response.half_agent.foreground.model = 'other-window writer';
    const rebased = rebaseSettings(saved, draft, response, 'full_agent', { 'model.model': 'new full' });
    expect(rebased.full_agent.model.model).toBe('new full');
    expect(rebased.half_agent.foreground.model).toBe('other-window writer');
  });
  it('keeps a preset list edited while an earlier collection save is in flight', () => {
    const saved = fixture();
    saved.shared.preference_presets = [{ id: 'slow', name: '放缓节奏', content: '多用对白。' }];
    const sent = [{ id: 'slow', name: '对白优先', content: '多用对白。' }];
    const draft = clone(saved);
    draft.shared.preference_presets = [{ ...sent[0], content: '保留行动空间。' }];
    const response = clone(saved);
    response.shared.preference_presets = clone(sent);
    response.revisions.shared = 1;
    const rebased = rebaseSettings(saved, draft, response, 'shared', { preference_presets: sent });
    expect(rebased.shared.preference_presets).toEqual(draft.shared.preference_presets);
    expect(difference(response.shared, rebased.shared)).toEqual({
      preference_presets: draft.shared.preference_presets,
    });
  });
});
