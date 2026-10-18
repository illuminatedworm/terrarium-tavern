<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { sources } from '../../lib/recipeSources';
import { splitCharacterBlock } from '../../lib/recipeBlocks';
import MessageTypeSelect from './MessageTypeSelect.vue';
import type { SettingsObject } from '../../lib/settingsDocument';
import SettingsFields, { type SettingField } from './SettingsFields.vue';
import SavedText from './SavedText.vue';
import Icon from '../common/Icon.vue';
const props = defineProps<{
  recipe: SettingsObject;
  saved: SettingsObject;
  guard: (action: () => void) => void;
  textValue: (path: string) => string;
  editText: (path: string, value: string) => void;
  saveText: (path: string) => void;
  textBusy: string;
  textErrors: Record<string, string>;
}>();
const index = ref(0);
watch(
  () => props.recipe.blocks.length,
  (count) => {
    index.value = Math.max(0, Math.min(index.value, count - 1));
  },
);
const block = computed(() => props.recipe.blocks[index.value]);
const source = (value: string) => sources.find((s) => s.value === value);
const path = computed(() => `context_recipe.blocks.${index.value}.content`);
const fields: SettingField[] = [
  { path: 'source', label: '内容来源', options: sources.map((s) => ({ value: s.value, label: s.label })) },
  { path: 'id', label: '块标识', type: 'text' },
  { path: 'priority', label: '优先级' },
  { path: 'depth', label: '注入深度', optional: true, min: 0 },
  { path: 'budget', label: '超长提示阈值（tokens）', optional: true, min: 1 },
  { path: 'condition', label: '生效条件', type: 'text' },
  { path: 'enabled', label: '启用内容块', type: 'checkbox' },
  { path: 'required', label: '必需内容', type: 'checkbox' },
];
function move(delta: number) {
  props.guard(() => {
    const target = index.value + delta;
    if (target < 0 || target >= props.recipe.blocks.length) return;
    const [item] = props.recipe.blocks.splice(index.value, 1);
    props.recipe.blocks.splice(target, 0, item);
    index.value = target;
  });
}
function add() {
  props.guard(() => {
    props.recipe.blocks.push({
      id: 'custom_' + crypto.randomUUID().slice(0, 8),
      source: 'custom',
      role: 'system',
      enabled: true,
      content: '',
      depth: null,
      priority: 50,
      budget: null,
      required: false,
      condition: '',
    });
    index.value = props.recipe.blocks.length - 1;
  });
}
function remove() {
  props.guard(() => {
    props.recipe.blocks.splice(index.value, 1);
    index.value = Math.max(0, index.value - 1);
  });
}
function splitCharacter() {
  props.guard(() => {
    props.recipe.blocks.splice(index.value, 1, ...splitCharacterBlock(block.value, props.recipe.blocks));
  });
}
</script>
<template>
  <dl class="settings-notes">
    <div>
      <dt>作用范围</dt>
      <dd>新对话复制此编排，已有对话保持各自的编排。</dd>
    </div>
    <div>
      <dt>保存</dt>
      <dd>顺序和参数自动保存；自定义文本需点击绿色勾。</dd>
    </div>
    <div>
      <dt>注入深度</dt>
      <dd>留空时按列表顺序注入；0 表示放在末尾。</dd>
    </div>
    <div>
      <dt>system 连续</dt>
      <dd>
        部分接口（如 Google GenAI）不接受对话中途的 system 消息。发送时，开头资料中的 system
        消息集中在最前，其他角色的资料随后，两组内部保持列表顺序；每轮排法固定，不会反复移动。列表本身和带深度的正文块不受影响，实际顺序可在“上下文”查看。
      </dd>
    </div>
  </dl>
  <div class="settings-recipe-layout">
    <div class="settings-block-list">
      <button
        v-for="(item, i) in recipe.blocks"
        :key="i"
        :class="{ active: index === i, off: !item.enabled }"
        @click="guard(() => (index = Number(i)))"
      >
        <span class="settings-block-index">{{ Number(i) + 1 }} ·</span>
        {{ source(item.source)?.label || item.source }}</button
      ><button class="settings-block-add" @click="add"><Icon name="plus" :size="14" />添加内容块</button>
    </div>
    <div v-if="block" class="settings-block-editor">
      <header class="settings-block-head">
        <div>
          <strong>{{ source(block.source)?.label || block.source }}</strong>
          <p v-if="source(block.source)?.description">{{ source(block.source)?.description }}</p>
        </div>
        <div class="settings-block-actions">
          <button
            class="icon-button"
            aria-label="上移"
            title="上移"
            :disabled="index === 0"
            @click="move(-1)"
          >
            <Icon name="arrow-up" :size="16" /></button
          ><button
            class="icon-button"
            aria-label="下移"
            title="下移"
            :disabled="index === recipe.blocks.length - 1"
            @click="move(1)"
          >
            <Icon name="arrow-down" :size="16" /></button
          ><button
            class="icon-button settings-block-remove"
            aria-label="移除块"
            title="移除块"
            @click="remove"
          >
            <Icon name="trash" :size="16" />
          </button>
        </div>
      </header>
      <button v-if="block.source === 'character'" class="settings-block-split" @click="splitCharacter">
        拆分角色设定为独立块
      </button>
      <SettingsFields :value="block" :fields="fields" />
      <MessageTypeSelect v-model="block.message_type" :disabled="block.source === 'history'" />
      <SavedText
        v-if="block.source === 'custom' || !source(block.source)"
        :key="block.id"
        label="自定义内容"
        :model-value="textValue(path)"
        :saved-value="saved.blocks[index]?.content || ''"
        :saving="textBusy === 'half_agent.' + path"
        :error="textErrors['half_agent.' + path]"
        :guard="guard"
        @update:model-value="editText(path, $event)"
        @save="saveText(path)"
      />
    </div>
  </div>
</template>
