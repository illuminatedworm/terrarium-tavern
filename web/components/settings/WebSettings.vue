<script setup lang="ts">
import type { WebSettings } from '../../lib/settingsDocument';
import { webServiceLabel } from '../../lib/webSearch';
const props = defineProps<{ value: WebSettings }>();
function move(index: number, delta: number) {
  const order = [...props.value.order];
  const next = index + delta;
  [order[index], order[next]] = [order[next], order[index]];
  props.value.order = order;
}
</script>
<template>
  <div class="web-settings">
    <p class="muted">
      参谋 agent 与制卡 agent 共用这些服务和密钥，联网方式在各自设置中选择。其他 Agent 不使用这些工具。
    </p>
    <fieldset v-for="(name, index) in value.order" :key="name">
      <legend>
        {{ index + 1 }}. {{ webServiceLabel(name)
        }}{{ name === 'searxng' || name === 'duckduckgo' ? ' · 低级备选' : '' }}
      </legend>
      <div class="web-service-heading">
        <label class="settings-check"
          ><input
            v-model="value[name].enabled"
            type="checkbox"
            :aria-label="`启用 ${webServiceLabel(name)}`"
          />启用</label
        >
        <div>
          <button
            :disabled="index === 0"
            :aria-label="`上移 ${webServiceLabel(name)}`"
            @click="move(index, -1)"
          >
            上移</button
          ><button
            :disabled="index === value.order.length - 1"
            :aria-label="`下移 ${webServiceLabel(name)}`"
            @click="move(index, 1)"
          >
            下移
          </button>
        </div>
      </div>
      <template v-if="name === 'exa' || name === 'tavily'">
        <label
          >接入方式<select v-model="value[name].auth_mode" :aria-label="`${webServiceLabel(name)} 接入方式`">
            <option value="keyless">免 key · 无需账号</option>
            <option value="api_key">自有 API key · 支持免费层级</option>
          </select></label
        >
        <template v-if="value[name].auth_mode === 'api_key'">
          <label
            >API key<input
              v-model="value[name].api_key"
              type="password"
              autocomplete="new-password"
              :aria-label="`${webServiceLabel(name)} API key`"
              :placeholder="value[name].has_api_key ? '已保存密钥；留空不修改' : '填写服务商提供的 API key'"
          /></label>
          <button class="clear-key" @click="value[name].api_key = ''">清除已保存密钥</button>
        </template>
      </template>
      <label v-else-if="name === 'searxng'"
        >SearXNG 地址<input
          v-model="value.searxng.endpoint"
          type="url"
          placeholder="https://search.example.org"
      /></label>
    </fieldset>
    <label
      >单项服务超时（秒）<input v-model.number="value.timeout_seconds" type="number" min="5" max="120"
    /></label>
    <p class="muted">
      仅在服务失败时尝试下一项；正常零结果不会触发降级。SearXNG 未填写地址时跳过。网页读取保留本地
      HTML／文本读取作为最终备选，超过 8 MiB 明确报错。
    </p>
    <p class="muted">免 key 有服务商限额；自有 key 使用所属账号的额度，本应用不升级套餐。</p>
    <p class="muted">本地接入跟随「网络与代理 → 模型接口请求使用代理」，不跟随提供商开关。</p>
  </div>
</template>
<style scoped>
.web-settings,
fieldset,
label {
  display: grid;
  gap: 12px;
  min-width: 0;
}
fieldset {
  padding: 16px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}
.web-service-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.web-service-heading > div {
  display: flex;
  gap: 6px;
}
.settings-check {
  display: flex;
  align-items: center;
}
.clear-key {
  justify-self: start;
}
.muted {
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: 1.7;
  margin: 0;
}
</style>
