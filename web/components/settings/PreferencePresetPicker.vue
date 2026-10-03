<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { api } from '../../lib/api';
import type { PreferencePreset, SettingsDocument } from '../../lib/settingsDocument';

const props = defineProps<{ modelValue: string; revision: number; presets: PreferencePreset[] }>();
const emit = defineEmits<{
  'update:modelValue': [value: string];
  saved: [settings: SettingsDocument];
  manage: [];
}>();
const selectedId = ref('');
const selected = computed(() => props.presets.find((preset) => preset.id === selectedId.value));
const naming = ref(false);
const name = ref('');
const nameInput = ref<HTMLInputElement>();
const saveButton = ref<HTMLButtonElement>();
const saving = ref(false);
const error = ref('');
const notice = ref('');
watch(
  () => props.presets,
  () => {
    if (!selected.value) selectedId.value = '';
  },
);
watch(
  () => [props.modelValue, props.revision],
  () => {
    notice.value = '';
  },
);
async function apply(append: boolean) {
  if (!selected.value) return;
  const current = props.modelValue.trimEnd();
  emit(
    'update:modelValue',
    append && current ? `${current}\n\n${selected.value.content}` : selected.value.content,
  );
  await nextTick();
  notice.value = '已填入，请保存会话设置后生效。';
}
async function startSaving() {
  name.value = props.modelValue.trim().split('\n')[0].slice(0, 24);
  error.value = '';
  notice.value = '';
  naming.value = true;
  await nextTick();
  nameInput.value?.focus();
  nameInput.value?.select();
}
async function cancel() {
  naming.value = false;
  error.value = '';
  await nextTick();
  saveButton.value?.focus();
}
async function save() {
  if (saving.value || !name.value.trim() || !props.modelValue.trim()) return;
  // Capture the suffix before awaiting; a later edit must not change what is saved.
  const content = props.modelValue;
  const presetName = name.value.trim();
  saving.value = true;
  error.value = '';
  try {
    // Read first so adding an item never overwrites another window's saved list.
    const current: SettingsDocument = await api('/settings');
    const duplicate = current.shared.preference_presets.find(
      (preset) => preset.content.trim() === content.trim(),
    );
    if (duplicate) {
      emit('saved', current);
      selectedId.value = duplicate.id;
      notice.value = `此偏好已保存为“${duplicate.name}”。`;
    } else {
      const preset: PreferencePreset = { id: crypto.randomUUID(), name: presetName, content };
      const updated: SettingsDocument = await api(
        '/settings/shared',
        {
          expected_revision: current.revisions.shared,
          changes: { preference_presets: [...current.shared.preference_presets, preset] },
        },
        'PATCH',
      );
      emit('saved', updated);
      selectedId.value = preset.id;
      notice.value = '已保存为常用偏好。';
    }
    naming.value = false;
    await nextTick();
    saveButton.value?.focus();
  } catch (e) {
    error.value = String(e).replace(/^Error: /, '');
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="preference-picker" role="group" aria-label="本轮偏好的常用偏好">
    <div class="preference-picker-heading">
      <span>常用偏好</span>
      <button type="button" @click="emit('manage')">管理常用偏好</button>
    </div>
    <div class="preference-picker-controls">
      <select v-model="selectedId" aria-label="选择常用偏好" :disabled="!presets.length">
        <option value="">{{ presets.length ? '选择常用偏好' : '暂无常用偏好' }}</option>
        <option v-for="preset in presets" :key="preset.id" :value="preset.id">{{ preset.name }}</option>
      </select>
      <button type="button" :disabled="!selected" @click="apply(false)">替换</button>
      <button type="button" :disabled="!selected" @click="apply(true)">追加</button>
    </div>
    <button v-if="!naming" ref="saveButton" type="button" :disabled="!modelValue.trim()" @click="startSaving">
      保存为常用偏好
    </button>
    <form v-else class="preference-picker-save" @submit.prevent="save" @keydown.esc.prevent.stop="cancel">
      <label>偏好名称<input ref="nameInput" v-model="name" maxlength="80" :disabled="saving" /></label>
      <div>
        <button type="button" :disabled="saving" @click="cancel">取消</button>
        <button class="primary" type="submit" :disabled="saving || !name.trim() || !modelValue.trim()">
          {{ saving ? '正在保存…' : '保存常用偏好' }}
        </button>
      </div>
    </form>
    <p v-if="notice" class="preference-picker-notice" role="status">{{ notice }}</p>
    <p v-if="error" class="preference-picker-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.preference-picker {
  display: grid;
  gap: 8px;
  margin: 10px 0;
  min-width: 0;
}
.preference-picker-heading,
.preference-picker-save > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.preference-picker-heading span,
.preference-picker-save label {
  font-size: 12px;
  color: var(--text-secondary);
}
.preference-picker-heading button {
  padding: 2px 0;
  min-height: 28px;
  border: 0;
  background: transparent;
  color: var(--accent);
}
.preference-picker-controls {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 6px;
}
.preference-picker select,
.preference-picker input {
  width: 100%;
  min-width: 0;
  min-height: 34px;
  margin: 0;
}
.preference-picker button {
  font-size: 12px;
  white-space: nowrap;
}
.preference-picker > button {
  justify-self: start;
}
.preference-picker-controls button {
  padding: 6px 9px;
}
.preference-picker-save {
  display: grid;
  gap: 8px;
}
.preference-picker-save label {
  display: grid;
  gap: 6px;
  margin: 0;
}
.preference-picker-save > div {
  justify-content: flex-end;
}
.preference-picker-notice,
.preference-picker-error {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.preference-picker-notice {
  color: var(--success);
}
.preference-picker-error {
  color: var(--danger);
}
</style>
