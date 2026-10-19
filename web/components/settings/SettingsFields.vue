<script setup lang="ts">
import { inject, type ComputedRef } from 'vue';
import { getPath, setPath, type SettingsObject } from '../../lib/settingsDocument';
import SelectMenu from '../common/SelectMenu.vue';
export type SettingField = {
  path: string;
  label: string;
  min?: number;
  max?: number;
  step?: number | string;
  type?: 'text' | 'number' | 'checkbox';
  optional?: boolean;
  placeholder?: string;
  help?: string;
  disabled?: (value: SettingsObject) => boolean;
  menu?: boolean;
  options?: Array<{ value: string | number; label: string; disabled?: boolean }>;
};
// `rows` lists each field as a settings row: label and help on the left, the
// control on the right. Pages of independent switches read better this way than
// in the dense multi-column grid used for numeric parameters.
const props = defineProps<{
  value: SettingsObject;
  fields: SettingField[];
  errorPrefix?: string;
  layout?: 'grid' | 'rows';
}>();
const errors = inject<ComputedRef<Record<string, string>>>('settings-field-errors');
const scope = inject<ComputedRef<string>>('settings-scope');
const fieldError = (field: SettingField) =>
  errors?.value[(props.errorPrefix || scope?.value + '.') + field.path];
function update(field: SettingField, event: Event) {
  const input = event.target as HTMLInputElement;
  setPath(
    props.value,
    field.path,
    field.type === 'checkbox'
      ? input.checked
      : field.options || field.type === 'text'
        ? input.value
        : input.value === ''
          ? field.optional
            ? null
            : ''
          : Number(input.value),
  );
}
</script>
<template>
  <div :class="layout === 'rows' ? 'settings-field-rows' : 'settings-field-grid'">
    <component
      :is="field.menu ? 'div' : 'label'"
      v-for="field in fields"
      :key="field.path"
      :data-setting-path="field.path"
      :class="{
        'settings-select-field': field.menu,
        'settings-check': field.type === 'checkbox',
        'settings-check-help': field.type === 'checkbox' && field.help,
      }"
    >
      <span>{{ field.label }}</span>
      <SelectMenu
        v-if="field.menu && field.options"
        :model-value="String(getPath(value, field.path) ?? '')"
        :options="
          field.options.map((option) => {
            const [label, ...description] = option.label.split(' · ');
            return {
              ...option,
              value: String(option.value),
              label,
              description: description.join(' · ') || undefined,
            };
          })
        "
        :label="field.label"
        :invalid="!!fieldError(field)"
        :disabled="field.disabled?.(value)"
        @update:model-value="setPath(value, field.path, $event)"
      />
      <select
        v-else-if="field.options"
        :value="getPath(value, field.path)"
        :aria-label="field.label"
        :aria-invalid="!!fieldError(field)"
        :disabled="field.disabled?.(value)"
        @change="update(field, $event)"
      >
        <option
          v-for="option in field.options"
          :key="option.value"
          :value="option.value"
          :disabled="option.disabled"
        >
          {{ option.label }}
        </option>
      </select>
      <input
        v-else-if="field.type === 'checkbox'"
        type="checkbox"
        :checked="getPath(value, field.path)"
        :disabled="field.disabled?.(value)"
        @change="update(field, $event)"
      />
      <input
        v-else
        :type="field.type || 'number'"
        :value="getPath(value, field.path) ?? ''"
        :min="field.min"
        :max="field.max"
        :step="field.step || 1"
        :aria-invalid="!!fieldError(field)"
        :placeholder="field.placeholder ?? (field.optional ? '留空使用接口默认值' : '')"
        :disabled="field.disabled?.(value)"
        @input="update(field, $event)"
      />
      <small v-if="field.help">{{ field.help }}</small>
      <small v-if="fieldError(field)" class="settings-field-error">{{ fieldError(field) }}</small>
    </component>
    <slot />
  </div>
</template>
