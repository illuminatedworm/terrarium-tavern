<script setup lang="ts">
import { editorSkillPresets, type EditorSkillPreset } from '../../lib/cardAuthorSkills';
import { cardName, type EditorCard } from '../../lib/cardAuthor';
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';

defineProps<{ cards: EditorCard[]; selected: string; disabled?: boolean }>();
defineEmits<{ select: [ref: string]; use: [preset: EditorSkillPreset]; reference: [] }>();
</script>

<template>
  <section class="card-skills">
    <header>
      <CardWorkspaceIcon name="book" />
      <h2>内置技能</h2>
    </header>
    <p class="skills-intro">把常用编辑方法交给 Agent，让角色卡按项目的运行方式工作。</p>
    <label v-if="cards.length"
      >应用到<select
        :value="selected"
        aria-label="技能目标卡片"
        @change="$emit('select', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="card in cards" :key="card.ref" :value="card.ref">
          {{ cardName(card) }}
        </option>
      </select></label
    >
    <p v-else class="skills-reference">
      先引用一张角色卡，再选择适合的编辑流程。<button :disabled="disabled" @click="$emit('reference')">
        引用角色
      </button>
    </p>
    <article v-for="preset in editorSkillPresets" :key="preset.id" class="skill-choice">
      <h3>{{ preset.title }}</h3>
      <p>{{ preset.description }}</p>
      <p class="skill-scope">{{ preset.scope }}</p>
      <p class="skill-outcome">{{ preset.outcome }}</p>
      <button :disabled="disabled || !cards.length" @click="$emit('use', preset)">
        填入请求 <CardWorkspaceIcon name="up" />
      </button>
    </article>
    <footer>
      这两种流程来自内置的角色卡适配技能。选择后会填入输入框，你可以补充要求再发送。修改仅进入草稿，审阅后由你保存到书架。
    </footer>
  </section>
</template>

<style scoped>
.card-skills header {
  display: flex;
  justify-content: flex-start;
  align-items: center;
  gap: 10px;
  height: auto;
  padding: 0;
  border: 0;
  color: var(--accent-hover);
}
.card-skills header h2 {
  font: 600 22px/1.4 var(--font-ui);
  color: var(--text-primary);
  margin: 0;
}
.card-skills header svg {
  width: 23px;
  height: 23px;
}
.skills-intro {
  color: var(--text-secondary);
  font: 14px/1.8 var(--font-ui);
  margin: 12px 0 24px;
}
.card-skills label {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 12px;
  color: var(--text-muted);
}
.card-skills label select {
  flex: 1;
  min-width: 0;
  font-size: 13px;
}
.skill-choice {
  padding: 20px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.skill-choice h3 {
  font: 600 16px/1.5 var(--font-ui);
  margin: 0 0 9px;
}
.skill-choice p {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--text-secondary);
}
.skill-choice .skill-scope,
.skill-choice .skill-outcome {
  color: var(--text-muted);
  font-size: 12px;
}
.skill-choice button {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
  color: var(--accent-hover);
  background: transparent;
  border-color: var(--border-control);
  padding: 7px 11px;
  font-size: 12px;
}
.skill-choice button svg {
  width: 14px;
  height: 14px;
  transform: rotate(45deg);
}
.card-skills footer {
  font: 12px/1.8 var(--font-ui);
  color: var(--text-muted);
  padding-top: 20px;
}
.skills-reference {
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.8;
}
.skills-reference button {
  display: block;
  margin-top: 12px;
  font-size: 12px;
}
</style>
