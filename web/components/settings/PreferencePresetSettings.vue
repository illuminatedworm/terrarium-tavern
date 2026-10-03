<script setup lang="ts">
import type { PreferencePreset } from '../../lib/settingsDocument';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import { computed, nextTick, ref } from 'vue';

const props = defineProps<{
  presets: PreferencePreset[];
  dirty: boolean;
  saving: boolean;
  error?: string;
}>();
const emit = defineEmits<{ 'update:presets': [presets: PreferencePreset[]]; save: [] }>();
const invalid = computed(() => props.presets.some((preset) => !preset.name.trim() || !preset.content.trim()));
function edit(id: string, field: 'name' | 'content', value: string) {
  emit(
    'update:presets',
    props.presets.map((preset) => (preset.id === id ? { ...preset, [field]: value } : preset)),
  );
}
async function add() {
  const id = crypto.randomUUID();
  emit('update:presets', [...props.presets, { id, name: '', content: '' }]);
  await nextTick();
  document.getElementById(`preset-name-${id}`)?.focus();
}
</script>

<template>
  <div class="preference-preset-settings" data-setting-path="preference_presets">
    <div class="preset-settings-toolbar">
      <p class="muted">保存常用的创作偏好，在会话设置的“本轮偏好”中选取并复用。</p>
      <button type="button" @click="add">添加常用偏好</button>
    </div>
    <p v-if="!presets.length" class="preset-empty">
      暂无常用偏好。可以在这里添加，也可以将会话中的本轮偏好保存为常用偏好。
    </p>
    <section
      v-for="(preset, index) in presets"
      :key="preset.id"
      class="preset-editor"
      :aria-label="'常用偏好 ' + (index + 1)"
    >
      <div class="preset-editor-heading">
        <label :for="'preset-name-' + preset.id"
          >名称<input
            :id="'preset-name-' + preset.id"
            :value="preset.name"
            maxlength="80"
            placeholder="例如：放缓节奏"
            :aria-invalid="!preset.name.trim() || undefined"
            @input="edit(preset.id, 'name', ($event.target as HTMLInputElement).value)"
        /></label>
        <button
          type="button"
          class="danger-button"
          :aria-label="'删除常用偏好 ' + (preset.name || index + 1)"
          @click="
            emit(
              'update:presets',
              presets.filter((item) => item.id !== preset.id),
            )
          "
        >
          删除
        </button>
      </div>
      <label
        >偏好内容<ExpandableTextarea
          :model-value="preset.content"
          :label="'常用偏好内容 ' + (index + 1)"
          placeholder="填写可复用的创作偏好"
          @update:model-value="edit(preset.id, 'content', $event)"
      /></label>
    </section>
    <div class="preset-settings-save">
      <span>{{ dirty ? '有修改尚未保存' : '已保存' }}</span>
      <button class="primary" type="button" :disabled="!dirty || saving || invalid" @click="emit('save')">
        {{ saving ? '正在保存…' : '保存常用偏好' }}
      </button>
    </div>
    <p v-if="dirty && invalid" class="preset-validation">请填写每个常用偏好的名称和内容。</p>
    <p v-if="error && dirty" class="preset-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.preference-preset-settings {
  min-width: 0;
}
.preset-settings-toolbar,
.preset-editor-heading,
.preset-settings-save {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.preset-settings-toolbar p {
  margin: 0;
}
.preset-settings-toolbar button,
.preset-editor-heading button {
  flex: none;
}
.preset-empty {
  margin: 28px 0;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.7;
}
.preset-editor {
  display: grid;
  gap: 14px;
  padding: 22px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.preset-editor-heading {
  align-items: flex-end;
}
.preset-editor-heading label {
  flex: 1;
}
.preset-editor-heading button {
  min-height: 36px;
}
.preset-settings-save {
  justify-content: flex-end;
  margin-top: 20px;
}
.preset-settings-save span {
  color: var(--text-muted);
  font-size: 12px;
}
.preset-validation,
.preset-error {
  font-size: 13px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.preset-validation {
  color: var(--text-secondary);
}
.preset-error {
  color: var(--danger);
}
@media (max-width: 760px) {
  .preset-settings-toolbar {
    align-items: flex-start;
    flex-direction: column;
    gap: 10px;
  }
}
</style>
