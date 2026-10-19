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
    开启后，新建对话时可在普通 API 渠道勾选 Kemini
    实验对话；只有这类对话受影响，其头像带有梅紫色描边。对话创建后不能与普通对话互相转换，ChatGPT
    官方渠道不能开启。关闭此项后已有 Kemini 实验对话暂停生成，普通对话不受影响。文本修改从下一次生成生效。
    此功能不改变实际工具权限和模型思考档位，不保证服务商接受或改变审核结果。
  </p>
  <p class="muted">
    头部固定为：小说填充 → CLEAR → 创作身份与文风 →
    资料起始。四条均在所有应用消息之前，不参与配方、世界书或脚本的深度计算，应用规则不能插入其中。
    助手确认、自检与声明位于历史后，传输指令随后追加。下列十项文本均可编辑，保存内容直接用于请求。
  </p>
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
