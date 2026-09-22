import { effectScope, ref } from 'vue';
import { expect, it } from 'vitest';
import { useStoryHistory } from '../../web/lib/storyHistory';

const messages = (count: number) => Array.from({ length: count }, (_, i) => ({ id: String(i) }));

it('starts with 60 messages, expands in batches, and preserves the full history', () => {
  const scope = effectScope();
  scope.run(() => {
    const session = ref({ id: 'long', messages: messages(10000) });
    const view = useStoryHistory(() => session.value);
    expect(view.visible.value).toHaveLength(60);
    expect(view.hidden.value).toBe(9940);
    view.showEarlier();
    expect(view.visible.value).toHaveLength(120);
    view.collapse();
    expect(view.visible.value).toHaveLength(60);
    expect(session.value.messages).toHaveLength(10000);
    session.value = { id: 'short', messages: messages(3) };
    expect(view.visible.value).toHaveLength(3);
    expect(view.hidden.value).toBe(0);
  });
  scope.stop();
});

it('reveals a source without hiding other messages or resetting on a turn refresh', () => {
  const scope = effectScope();
  scope.run(() => {
    const session = ref({ id: 'story', messages: messages(300) });
    const view = useStoryHistory(() => session.value);
    view.reveal('10');
    expect(view.visible.value[0].id).toBe('10');
    view.reveal('missing');
    view.reveal('250');
    expect(view.hidden.value).toBe(10);
    session.value = { id: 'story', messages: messages(302) };
    expect(view.visible.value[0].id).toBe('10');
    expect(view.visible.value.at(-1)?.id).toBe('301');
    view.collapse();
    expect(view.visible.value).toHaveLength(60);
  });
  scope.stop();
});
