<script setup lang="ts">
import Icon from '../common/Icon.vue';
import { useTimedError } from '../../composables/useTimedError';
import { onMounted, ref } from 'vue';
import { api } from '../../lib/api';
const clone = (value: any) => JSON.parse(JSON.stringify(value));
const emit = defineEmits<{ back: [] }>();
const items = ref<any[]>([]),
  draft = ref<any>({ name: '', description: '' }),
  error = ref(''),
  notice = ref('');
async function load() {
  items.value = await api('/personas');
}
async function save() {
  error.value = '';
  try {
    draft.value = await api(
      draft.value.id ? `/personas/${draft.value.id}` : '/personas',
      {
        name: draft.value.name,
        description: draft.value.description,
        expected_version: draft.value.version || 0,
      },
      draft.value.id ? 'PUT' : 'POST',
    );
    await load();
    notice.value = '已保存；新故事可以选择这个用户角色。';
  } catch (e: any) {
    error.value = e.message;
  }
}
async function remove() {
  error.value = '';
  try {
    await api(`/personas/${draft.value.id}`, undefined, 'DELETE');
    draft.value = { name: '', description: '' };
    await load();
    notice.value = '已永久删除用户角色；已有故事保留其独立身份快照。';
  } catch (e: any) {
    error.value = e.message;
  }
}
onMounted(load);
useTimedError(error);
</script>
<template>
  <section class="editor-page">
    <div class="editor-header">
      <div>
        <button class="quiet editor-back" @click="emit('back')">
          <Icon name="arrow-left" :size="16" />返回
        </button>
        <h1>用户角色</h1>
        <p class="muted">名字之外，也写下外貌、身份、性格、能力、背景和希望保留的边界。</p>
      </div>
      <button v-if="draft.id" @click="remove">永久删除用户角色</button
      ><button class="primary" @click="save">保存用户角色</button>
    </div>
    <AppNotification :message="error" />
    <AppNotification :message="notice" />
    <div class="editor-layout">
      <div class="editor-fields">
        <label>名字<input v-model="draft.name" placeholder="角色的称呼" /></label
        ><label
          >完整描述<textarea
            v-model="draft.description"
            rows="22"
            placeholder="例如：你的身份与背景、外貌、说话习惯、与故事人物的关系、能力及限制…"
          />
        </label>
      </div>
      <aside class="editor-start">
        <button
          @click="
            draft = { name: '', description: '' };
            notice = '';
          "
        >
          <Icon name="plus" :size="16" />新用户角色</button
        ><button
          v-for="p in items"
          class="persona-item"
          @click="
            draft = clone(p);
            notice = '';
          "
        >
          {{ p.name }}
        </button>
      </aside>
    </div>
  </section>
</template>
