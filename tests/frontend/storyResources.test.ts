import { expect, it } from 'vitest';
import { runInNewContext } from 'node:vm';
import { acquireAsset, acquireHistory, acquireMarkup, reservePanelState } from '../../web/lib/storyAssets';
import { frameCapabilities } from '../../compat/frame-capabilities';
import { createPresentationCache, responseData, storyPresentation } from '../../web/lib/storyPresentation';

it('shares immutable blobs until the last consumer releases them', () => {
  const first = acquireAsset('test', () => new Blob(['large asset']));
  const second = acquireAsset('test', () => {
    throw Error('duplicate');
  });
  expect(first.blob).toBe(second.blob);
  first.release();
  first.release();
  const third = acquireAsset('test', () => {
    throw Error('premature release');
  });
  expect(third.blob).toBe(second.blob);
  second.release();
  third.release();
  const next = acquireAsset('test', () => new Blob(['replacement']));
  expect(next.blob).not.toBe(first.blob);
  next.release();
});

it('shares history source while preserving complete synchronous reads and private objects', async () => {
  const messages = [
    {
      id: 'a',
      role: 'assistant',
      raw: '完整原文</script>',
      snapshot: { state: { stat_data: { hp: 1 }, old: true } },
    },
    { id: 'b', role: 'user', raw: '第二条', snapshot: { state: { stat_data: { hp: 2 } } } },
  ];
  const first = acquireHistory(messages),
    second = acquireHistory(messages);
  expect(first.blob).toBe(second.blob);
  const context = { window: {} as any };
  runInNewContext(await first.blob.text(), context);
  const source = context.window.__tavernHistory;
  expect(source.read(0).message).toBe(messages[0].raw);
  const value = source.read(0);
  value.variables.stat_data.hp = 999;
  expect(source.read(0).variables.stat_data.hp).toBe(1);
  expect(source.variables(1)).toEqual({ stat_data: { hp: 2 }, old: true });
  first.release();
  second.release();
});

it('extracts repeated media and anonymous CSS but leaves script strings and named styles intact', () => {
  const uri = 'data:image/svg+xml;base64,' + btoa('<svg><!--' + 'a'.repeat(2200) + '--></svg>');
  const input = `<style>.a{background:url('${uri}')}</style><img src="${uri}"><script>const original="${uri}"</script><style id="dynamic">x{color:red}</style>`;
  const first = acquireMarkup(input),
    second = acquireMarkup(input);
  expect(first.html).toContain('<link rel="stylesheet"');
  expect(first.html).toContain(`<script>const original="${uri}"</script>`);
  expect(first.html).toContain('<style id="dynamic">');
  expect(first.leases[0].blob).toBe(second.leases[0].blob);
  expect(first.leases[0].blob).toBe(first.leases[2].blob);
  first.release();
  second.release();
});

it('bounds saved UI state without evicting live state, including invalid save results', () => {
  const saved = reservePanelState({ text: '用户未提交输入' }, 100);
  expect(saved).toBeDefined();
  expect(reservePanelState('x'.repeat(100), 100)).toBeUndefined();
  saved!.release();
  saved!.release();
  const next = reservePanelState('x'.repeat(90), 100);
  expect(next).toBeDefined();
  next!.release();
  const cyclic: any = {};
  cyclic.self = cyclic;
  expect(reservePanelState(cyclic)).toBeUndefined();
});

it('requires actual capability declarations, never examples inside scripts or comments', () => {
  const meta = '<meta name="tavern-capabilities" content="display-only live-state suspend shared-assets">';
  expect(frameCapabilities(meta).displayOnly).toBe(true);
  expect(frameCapabilities(`<script>const sample='${meta}'</script><!--${meta}-->`).displayOnly).toBe(false);
});

it('reuses unchanged presentation, bounds the cache, and preserves CRLF source boundaries', () => {
  const parse = createPresentationCache(1000, 2);
  const first = parse('one');
  expect(parse('one')).toBe(first);
  parse('two');
  parse('three');
  expect(parse('one')).not.toBe(first);
  const text = '前文。\r\n\r\n<div>状态</div>\r\n\r\n尾文。';
  expect(
    storyPresentation(text)
      .body.map((part) => part.text)
      .join(''),
  ).toBe(text);
  const snapshot = { state: { stat_data: { hp: 4 } } };
  const message = { raw: 'raw', display: 'same', role: 'assistant', snapshot };
  const a: any = responseData({ messages: [message] }),
    b: any = responseData({ messages: [{ ...message }] });
  expect(a.messages[0].presentation).toBe(b.messages[0].presentation);
  expect(a.messages[0].snapshot).toBe(snapshot);
  expect((message as any).presentation).toBeUndefined();
});
