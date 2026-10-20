<script setup lang="ts">
import Icon from './Icon.vue';
</script>

<template>
  <div class="about-mark" aria-hidden="true"><Icon name="brand" :size="30" :stroke="1.4" /></div>
  <div class="eyebrow">Terrarium Tavern · 0.1</div>
  <h2 class="about-title">给故事一处生长的地方。</h2>
  <p>本地优先的 Roleplay Agent 应用。角色数据、剧情分支和记忆保存在你的电脑。</p>
  <p>
    Powered by
    <a href="https://github.com/Kohaku-Lab/KohakuTerrarium" target="_blank" rel="noreferrer"
      >KohakuTerrarium</a
    >
    · Shih-Ying Yeh 与贡献者。
  </p>
  <p>兼容接口参考 Tavern Helper 与 MagVarUpdate。支持范围以项目兼容矩阵和测试为准。</p>
</template>
