<script setup lang="ts">
import SettingsFields from './SettingsFields.vue';
import SavedText from './SavedText.vue';
import { keminiFields, keminiTextFields } from '../../lib/settingsFields';
import type { SettingsObject } from '../../lib/settingsDocument';
defineProps<{
  value: SettingsObject;
  saved: SettingsObject;
  textValue: (path: string) => string;
  editText: (path: string, text: string) => void;
  saveText: (path: string) => void;
  textBusy: string;
  textErrors: Record<string, string>;
  guard: (action: () => void) => void;
}>();
</script>

<template>
  <h4>Kemini 式实验功能</h4>
  <SettingsFields :value="value" :fields="keminiFields" layout="rows" />
  <p class="muted">
    仅支持普通 API 渠道，需在新建对话时选择 Kemini
    实验模式，创建后不可切换。关闭后暂停实验对话生成，普通对话不受影响。
  </p>
  <p class="muted">可在下方编辑创作提示与小说填充，保存后从下一次生成生效。</p>
  <p v-if="value.kemini.enabled && !saved.kemini.filler_text.trim()" role="status" class="muted">
    未配置填充文本：头部省略填充，其余已保存提示仍生效。
  </p>
  <details class="settings-advanced">
    <summary>编辑实验提示与填充文本</summary>
    <div v-for="field in keminiTextFields" :key="field.path" :data-setting-path="field.path">
      <SavedText
        :label="field.label"
        :model-value="textValue(field.path)"
        :saved-value="saved.kemini[field.path.split('.')[1]]"
        :saving="textBusy === 'shared.' + field.path"
        :error="textErrors['shared.' + field.path]"
        :guard="guard"
        @update:model-value="editText(field.path, $event)"
        @save="saveText(field.path)"
      />
      <p class="muted">{{ field.help }}</p>
      <p v-if="field.path === 'kemini.filler_text'" class="muted">
        {{ Array.from(textValue(field.path)).length }} 字符；实际请求的 token
        估算见上下文预览。填充不会自动补写或截断。
      </p>
    </div>
  </details>
</template>
