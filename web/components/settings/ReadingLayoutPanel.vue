<script setup lang="ts">
import { clampRightWidth, type ReadingLayout } from '../../lib/dataTransfer';

/** 阅读排版: browser-local reading preferences, applied immediately. */
const layout = defineModel<ReadingLayout>('layout', { required: true });
type NumericKey = 'fontSize' | 'lineHeight' | 'readingWidth';
function normalize(key: NumericKey, min: number, max: number) {
  const value = Number(layout.value[key]);
  layout.value[key] = Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : min;
}
</script>

<template>
  <h2>阅读排版</h2>
  <p class="muted">设置即时生效，保存在当前浏览器。</p>
  <p class="layout-preview" aria-hidden="true">
    雾气从港口的石阶上缓缓爬起。“你来晚了。”守卫靠在铁栏边，提灯晃出一圈昏黄的光晕。
  </p>
  <div class="layout-grid">
    <label
      >正文字号<input
        aria-label="正文字号"
        type="number"
        min="14"
        max="28"
        v-model.number="layout.fontSize"
        @change="normalize('fontSize', 14, 28)" /></label
    ><label
      >行距<input
        aria-label="行距"
        type="number"
        min="1.4"
        max="2.6"
        step="0.1"
        v-model.number="layout.lineHeight"
        @change="normalize('lineHeight', 1.4, 2.6)" /></label
    ><label
      >阅读区宽度<input
        aria-label="阅读区宽度"
        type="number"
        min="700"
        max="1600"
        step="10"
        v-model.number="layout.readingWidth"
        @change="normalize('readingWidth', 700, 1600)" /></label
    ><label title="桌面窗口中最多占窗口宽度的 35%，优先保证正文栏"
      >右侧栏宽度<input
        aria-label="右侧栏宽度"
        type="number"
        min="240"
        max="640"
        step="10"
        v-model.number="layout.rightWidth"
        @change="layout.rightWidth = clampRightWidth(Number(layout.rightWidth) || 0)" /></label
    ><label
      >字体<select v-model="layout.fontFamily">
        <option value="sans">清晰无衬线</option>
        <option value="serif">书籍衬线</option>
      </select></label
    >
  </div>
  <label class="check"><input type="checkbox" v-model="layout.leftCollapsed" />折叠左侧栏</label
  ><label class="check"><input type="checkbox" v-model="layout.rightCollapsed" />折叠右侧栏</label
  ><label class="check" title="关闭后界面改用不透明面板，适合低性能显卡或偏好清晰边界"
    ><input type="checkbox" v-model="layout.glass" />毛玻璃效果</label
  >
</template>
