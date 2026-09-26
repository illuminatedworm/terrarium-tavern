<script setup lang="ts">
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
defineProps<{ settings: any }>();
</script>
<template>
  <section aria-label="Full Agent 设置">
    <h3>Full Agent</h3>
    <p>一个 Agent 负责写作、检索和记忆。模型通过工具保存笔记、查找原文并主动换窗。</p>
    <label><input type="checkbox" v-model="settings.full_agent_config.auto_retrieval" />自动检索世界书</label>
    <p class="muted">
      开启后在每轮开始时自动检索。Agent 仍可自行查询同一索引，所有查询串行执行，主动查询优先。
    </p>
    <label
      >每回合模型调用上限<input
        type="number"
        min="2"
        max="100"
        v-model.number="settings.full_agent_config.max_steps"
    /></label>
    <label
      >窗口压力提醒阈值（0.25–0.9）<input
        type="number"
        min="0.25"
        max="0.9"
        step="0.05"
        v-model.number="settings.full_agent_config.pressure_ratio"
    /></label>
    <label
      >交接笔记上限（tokens）<input
        type="number"
        min="128"
        max="16384"
        v-model.number="settings.full_agent_config.checkpoint_tokens"
    /></label>
    <label
      >单次工具结果上限（tokens）<input
        type="number"
        min="256"
        max="8192"
        v-model.number="settings.full_agent_config.tool_result_tokens"
    /></label>
    <label
      >单条长期笔记上限（tokens）<input
        type="number"
        min="128"
        max="16384"
        v-model.number="settings.full_agent_config.note_tokens"
    /></label>
    <label
      >Full Agent 系统提示词<ExpandableTextarea
        v-model="settings.full_agent_system_prompt"
        label="Full Agent 系统提示词"
    /></label>
    <p class="muted">
      Full Agent 固定使用随对话注入世界书，不使用 Half Agent 的历史裁剪、分类记忆、周期整理或上下文配方。
    </p>
  </section>
</template>
