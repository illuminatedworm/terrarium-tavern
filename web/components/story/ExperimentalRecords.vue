<script setup lang="ts">
import { auxiliaryLabels, type AuxiliaryPart } from '../../lib/kemini';
defineProps<{ parts: AuxiliaryPart[] }>();
</script>

<template>
  <details v-if="parts.length" class="experimental-records">
    <summary>实验辅助记录 · {{ parts.length }}</summary>
    <p class="muted">生成时的记录，不参与后续剧情上下文；编辑正文不会修改这些记录。</p>
    <section v-for="(part, index) in parts" :key="index">
      <strong>{{ auxiliaryLabels[part.kind] }}</strong>
      <pre>{{ part.text }}</pre>
    </section>
  </details>
</template>
