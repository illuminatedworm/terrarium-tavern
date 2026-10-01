<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { sources } from '../../lib/recipeSources';
import { splitCharacterBlock } from '../../lib/recipeBlocks';
import type { SettingsObject } from '../../lib/settingsDocument';
import SettingsFields, { type SettingField } from './SettingsFields.vue';
import SavedText from './SavedText.vue';
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
const path = computed(() => `context_recipe.blocks.${index.value}.content`);
const fields: SettingField[] = [
  { path: 'source', label: '内容来源', options: sources.map((s) => ({ value: s.value, label: s.label })) },
  { path: 'id', label: '块标识', type: 'text' },
  {
    path: 'role',
    label: '消息角色',
    options: [
      { value: 'system', label: '系统指令' },
      { value: 'developer', label: '开发者指令' },
      { value: 'user', label: '用户发言' },
      { value: 'assistant', label: '模型回复' },
    ],
  },
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
  <p class="muted">新对话复制此编排；已有对话保持原编排。顺序和参数自动保存，文本修改需点击绿色勾。</p>
  <p class="muted">每块可单独上移、下移或设置深度；深度留空时按列表顺序注入，0 放在末尾。</p>
  <div class="settings-recipe-layout">
    <div class="settings-block-list">
      <button
        v-for="(item, i) in recipe.blocks"
        :key="i"
        :class="{ active: index === i }"
        @click="guard(() => (index = Number(i)))"
      >
        {{ Number(i) + 1 }} · {{ sources.find((s) => s.value === item.source)?.label || item.source }}</button
      ><button @click="add">添加内容块</button>
    </div>
    <div v-if="block">
      <div class="profile-actions">
        <button :disabled="index === 0" @click="move(-1)">上移</button
        ><button :disabled="index === recipe.blocks.length - 1" @click="move(1)">下移</button
        ><button @click="remove">移除块</button>
      </div>
      <p class="muted">{{ sources.find((s) => s.value === block.source)?.description }}</p>
      <button v-if="block.source === 'character'" @click="splitCharacter">拆分角色设定为独立块</button>
      <SettingsFields :value="block" :fields="fields" />
      <SavedText
        v-if="block.source === 'custom' || !sources.some((s) => s.value === block.source)"
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
