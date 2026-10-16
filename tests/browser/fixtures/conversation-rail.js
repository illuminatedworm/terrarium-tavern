import { createApp, h, nextTick, ref } from 'vue';
import Rail from '../../../web/components/story/ConversationRail.vue';
import History from '../../../web/components/story/StoryHistory.vue';
import DialogueTools from '../../../web/components/story/DialogueTools.vue';
import '../../../web/styles/tokens.css';
import '../../../web/styles/base.css';
import '../../../web/styles/story.css';

const messages = ref([]);
const reading = ref('');
const breakpoint = ref('');
const scroller = ref();
const history = ref();
const calls = [];
function seed(count) {
  messages.value = Array.from({ length: count }, (_, i) => [
    { id: `u${i}`, role: 'user', raw: `第 ${i + 1} 轮：` + '这是一段很长的用户输入。'.repeat(20) },
    {
      id: `a${i}`,
      parent: `u${i}`,
      role: 'assistant',
      raw: `回复 ${i + 1}。` + '这是可变长度正文。'.repeat(20 + (i % 10) * 10),
    },
  ]).flat();
  reading.value = 'u0';
  breakpoint.value = `a${count - 2}`;
  calls.length = 0;
}
window.railFixture = {
  seed,
  calls,
  reading,
  async jump(index) {
    await history.value.jumpTo(`u${index}`);
  },
  setReading(index) {
    reading.value = `u${index}`;
  },
};
seed(5000);
createApp({
  setup() {
    return () =>
      h('div', { class: 'rail-fixture' }, [
        h('div', { class: 'fixture-scroller', ref: scroller, tabindex: 0 }, [
          h(
            History,
            {
              ref: history,
              messages: messages.value,
              scroller: scroller.value,
              onReading: (id) => (reading.value = id),
            },
            {
              default: ({ message }) =>
                h(
                  'article',
                  {
                    class: 'message',
                    'data-id': message.id,
                    style: message.id === messages.value.at(-1)?.id ? { minHeight: '1500px' } : undefined,
                  },
                  message.raw,
                ),
            },
          ),
        ]),
        h(DialogueTools, {
          messages: messages.value,
          scroller: scroller.value,
          active: true,
          navigate: async (id) => {
            await history.value.jumpTo(id);
          },
          releaseAnchor: () => history.value.releaseAnchor(),
          onBoundary: (edge) => history.value.jumpBoundary(edge),
        }),
        h(Rail, {
          messages: messages.value,
          readingId: reading.value,
          breakpointId: breakpoint.value,
          onNavigate: async (id) => {
            calls.push(id);
            await nextTick();
            await history.value.jumpTo(id);
          },
        }),
      ]);
  },
}).mount('#app');
const style = document.createElement('style');
style.textContent = `
  body { margin: 0; }
  .rail-fixture { position: relative; width: 100vw; height: calc(100vh - 180px); }
  .fixture-scroller { height: 100%; width: calc(100% - 60px); overflow: auto; }
  .fixture-scroller .message { display: block; margin: 0 0 24px; padding: 16px; line-height: 28px; }
`;
document.head.append(style);
