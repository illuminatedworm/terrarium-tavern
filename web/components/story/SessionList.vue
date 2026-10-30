<script setup lang="ts">
import { computed, nextTick, ref } from 'vue';
import { novelActivity } from '../../lib/novel';
import Icon from '../common/Icon.vue';
import CharacterPortrait from '../character/CharacterPortrait.vue';
import { channelLabels, type Channel } from '../../lib/channels';
import type { SessionSummary } from '../../lib/types';

/** Left sidebar list of stories with inline renaming. */
const props = defineProps<{
  sessions: SessionSummary[];
  activeId?: string;
  portraits: Record<string, string>;
  busy: boolean;
  saving: boolean;
  /** Resolves true once saved; a failure keeps the editor open with the draft. */
  rename: (item: SessionSummary, title: string) => Promise<boolean>;
}>();
const emit = defineEmits<{
  'new-story': [];
  open: [id: string];
  delete: [item: SessionSummary, event: MouseEvent];
  invalid: [message: string];
}>();

const editingId = ref('');
const investigators = computed(() => {
  const counts: Record<string, number> = {};
  for (const job of novelActivity.value) counts[job.session] = (counts[job.session] || 0) + 1;
  return counts;
});
const draft = ref('');
const input = ref<HTMLInputElement | null>(null);

function edit(item: SessionSummary) {
  editingId.value = item.id;
  draft.value = item.title || item.name || '';
  nextTick(() => {
    input.value?.focus();
    input.value?.select();
  });
}
function cancel() {
  editingId.value = '';
  draft.value = '';
}
async function save(item: SessionSummary) {
  if (props.saving) return;
  const title = draft.value.trim();
  if (!title || title.length > 100) {
    emit('invalid', '对话标题须为 1 到 100 个字符');
    return;
  }
  if (title === item.title || (await props.rename(item, title))) cancel();
}
// The second click of a double click (which renames) must not reopen the story.
function open(event: MouseEvent, item: SessionSummary) {
  if (event.detail < 2) emit('open', item.id);
}
</script>

<template>
  <header class="panel-head">
    <h2>会话</h2>
    <span class="panel-count">{{ sessions.length }}</span>
  </header>
  <button class="new-story" :disabled="busy" @click="emit('new-story')">
    <Icon name="plus" :size="17" />开始新故事
  </button>
  <nav class="session-list" aria-label="会话列表">
    <div
      v-for="s in sessions"
      :key="s.id"
      class="session-row"
      :class="{ active: activeId === s.id, kemini: s.kemini }"
    >
      <div v-if="editingId === s.id" class="session-title-editor">
        <input
          :ref="(element) => (input = element as HTMLInputElement | null)"
          v-model="draft"
          maxlength="100"
          aria-label="对话标题"
          @keydown.enter="save(s)"
          @keydown.esc="cancel"
        />
        <button :disabled="saving" @click="save(s)">保存</button>
        <button :disabled="saving" @click="cancel">取消</button>
      </div>
      <template v-else
        ><button
          class="session-open"
          title="单击打开对话，双击重命名"
          @click="open($event, s)"
          @dblclick.stop.prevent="edit(s)"
        >
          <CharacterPortrait :src="portraits[s.id]" :name="s.name" />
          <span
            >{{ s.title || s.name
            }}<small
              >{{ new Date(s.created * 1000).toLocaleDateString('zh-CN') }} · {{ s.persona }} ·
              {{ channelLabels[s.channel_origin as Channel] || '渠道待确认'
              }}{{ investigators[s.id] ? ` · 原著调查者 ${investigators[s.id]}` : '' }}</small
            ></span
          ></button
        ><span class="row-actions"
          ><button
            class="session-rename"
            title="重命名"
            :aria-label="`重命名对话 ${s.title || s.name}`"
            @click.stop="edit(s)"
          >
            <Icon name="pencil" :size="15" /></button
          ><button
            class="session-delete"
            title="删除"
            :aria-label="`删除会话 ${s.title || s.name}`"
            @click.stop="emit('delete', s, $event)"
          >
            <Icon name="trash" :size="15" /></button></span
      ></template>
    </div>
    <p v-if="!sessions.length" class="empty-small">故事开始后，会留在这里。</p>
  </nav>
</template>
