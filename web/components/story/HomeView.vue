<script setup lang="ts">
import { computed } from 'vue';
import Icon from '../common/Icon.vue';
import CharacterPortrait from '../character/CharacterPortrait.vue';
import { channelLabels, type Channel } from '../../lib/channels';
import type { SessionSummary } from '../../lib/types';

/** Landing page shown while no story is open. */
const props = defineProps<{
  sessions: SessionSummary[];
  portraits: Record<string, string>;
  cardCount: number;
  busy: boolean;
}>();
const emit = defineEmits<{ 'new-story': []; 'browse-cards': []; open: [id: string] }>();
// Home shows the newest stories first; the sidebar keeps the full list.
const recentSessions = computed(() =>
  [...props.sessions].sort((a, b) => (b.created || 0) - (a.created || 0)).slice(0, 6),
);
</script>

<template>
  <section class="library home">
    <div class="home-hero">
      <div class="home-emblem" aria-hidden="true"><Icon name="brand" :size="34" :stroke="1.4" /></div>
      <div class="eyebrow">A place for your stories</div>
      <h1>推开门，<br />进入另一个世界。</h1>
      <p class="intro">让角色拥有记忆，让每一个选择留下回响。从一张角色卡，开始属于你的故事。</p>
      <div class="home-actions">
        <button v-if="cardCount" class="primary" :disabled="busy" @click="emit('new-story')">
          <Icon name="sparkle" :size="17" />开始一段新故事
        </button>
        <button @click="emit('browse-cards')">
          <Icon :name="cardCount ? 'cards' : 'import'" :size="17" />{{
            cardCount ? `浏览角色卡 · ${cardCount}` : '前往导入角色卡'
          }}
        </button>
      </div>
    </div>
    <section v-if="recentSessions.length" aria-label="最近的故事">
      <div class="library-heading">
        <h2>最近的故事</h2>
        <span>共 {{ sessions.length }} 个会话</span>
      </div>
      <div class="recent-grid">
        <button
          v-for="s in recentSessions"
          :key="s.id"
          class="recent-card"
          :class="{ kemini: s.kemini }"
          @click="emit('open', s.id)"
        >
          <CharacterPortrait :src="portraits[s.id]" :name="s.name" size="card" />
          <span
            ><strong>{{ s.title || s.name }}</strong
            ><small>{{ new Date(s.created * 1000).toLocaleDateString('zh-CN') }} · {{ s.persona }}</small
            ><small>{{ channelLabels[s.channel_origin as Channel] || '渠道待确认' }}</small></span
          >
        </button>
      </div>
    </section>
    <div v-else class="library-guide">
      <span class="guide-mark"><Icon :name="cardCount ? 'cards' : 'import'" :size="20" /></span>
      <div>
        <strong>{{ cardCount ? '从一张角色卡开始' : '先导入一张角色卡' }}</strong>
        <p>
          {{
            cardCount
              ? '点击“开始一段新故事”选择角色卡，设置你的身份与开场，即可进入故事。'
              : '角色卡在左侧“角色卡”分类导入，支持 JSON、PNG 与 CHARX。'
          }}
        </p>
      </div>
    </div>
    <ul class="home-tips">
      <li>
        <Icon name="branch" :size="18" /><span
          ><strong>剧情可以分支</strong>从任意回复继承进度另开对话，原来的故事依然保留。</span
        >
      </li>
      <li>
        <Icon name="memory" :size="18" /><span
          ><strong>记忆在后台整理</strong>Tracker 随剧情维护记忆与变量，长对话也能记得来龙去脉。</span
        >
      </li>
      <li>
        <Icon name="compass" :size="18" /><span
          ><strong>参谋随时商量</strong>在右侧栏与参谋 agent 讨论走向，它只读故事，不改正文。</span
        >
      </li>
    </ul>
  </section>
</template>
