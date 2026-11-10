<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import FieldHelp from '../common/FieldHelp.vue';
import Icon from '../common/Icon.vue';

/** Inspector "缓存" page. One fixed layout for every channel and interface: sections
 *  the current connection cannot use stay in place and say why, with the way out. */
const props = defineProps<{
  session: any;
  breakpointId: string | null;
  requestState?: any;
  sentLabel: string;
  disabled: boolean;
  retention: {
    model: string;
    apiLabel: string;
    protocolNote: string;
    value: string;
    enabled: boolean;
    explicit: boolean;
    note: string;
    error?: string;
    options: { value: string; label: string }[];
    channel: string;
    role: string;
    preset: string;
    control: 'subscription' | 'implicit' | 'cloud' | 'runtime';
  };
  runtime: boolean;
}>();
const emit = defineEmits<{
  save: [automatic: boolean, messageId: string | null, interval: number, offset: number];
  locate: [];
  'save-retention': [value: string];
  settings: [];
}>();
const automatic = ref(true);
const interval = ref(1);
const offset = ref(2);
function reset() {
  automatic.value = props.session.cache_breakpoint_auto;
  interval.value = props.session.cache_breakpoint_follow?.interval || 1;
  offset.value = props.session.cache_breakpoint_follow?.offset || 2;
}
watch(() => [props.session.id, props.session.revision], reset, { immediate: true });
const changed = computed(
  () =>
    automatic.value !== props.session.cache_breakpoint_auto ||
    interval.value !== props.session.cache_breakpoint_follow?.interval ||
    offset.value !== props.session.cache_breakpoint_follow?.offset,
);
const plannedIndex = computed(() =>
  props.session.messages.findIndex((message: any) => message.id === props.breakpointId),
);
const plannedRef = computed(() => (plannedIndex.value < 0 ? '固定前缀' : `H${plannedIndex.value + 1}`));
const retentionLabel = computed(
  () => props.retention.options.find((option) => option.value === props.retention.value)?.label || '',
);
const status = computed(() => {
  const { control, explicit } = props.retention;
  if (props.retention.error) return { badge: '配置待修正', summary: '缓存时效与当前模型不兼容' };
  if (control === 'subscription') return { badge: '服务端管理', summary: '服务端管理' };
  if (control === 'implicit') return { badge: '仅隐式', summary: '服务端隐式缓存' };
  const timing = props.retention.enabled && retentionLabel.value ? `时效 ${retentionLabel.value}` : '';
  if (props.runtime)
    return {
      badge: '本地断点',
      live: true,
      summary: ['本地断点', timing || '无时效参数', `计划 ${plannedRef.value}`].join(' · '),
    };
  const badge = control === 'runtime' && !explicit ? '服务端缓存' : '云端管理';
  return { badge, summary: [badge, timing].filter(Boolean).join(' · ') };
});
const controlLabel = computed(() => {
  const { control, preset, explicit } = props.retention;
  if (control === 'cloud') return '云端默认';
  if (control !== 'runtime') return '';
  return '本地运行时控制' + (preset === 'custom' ? ' · 自定义参数' : !explicit ? ' · 自动格式' : '');
});
const retentionOff = computed(() => {
  const { control, options } = props.retention;
  if (control === 'subscription') return '订阅通道不支持显式缓存控制，时效由服务端决定。';
  if (control === 'implicit') return props.retention.note;
  return options.length ? '' : '当前接入方式没有可选的缓存时效参数。';
});
const breakpointOff = computed(() => {
  const { control } = props.retention;
  if (control === 'subscription') return { text: '订阅通道不发送本地断点。' };
  if (control === 'implicit') return { text: '隐式缓存不使用本地断点。' };
  if (control === 'cloud') return { text: '云端默认不发送本地断点。', link: '切换为本地运行时控制' };
  return { text: '自动格式下由服务端匹配缓存前缀，不发送断点。', link: '改为自定义参数' };
});
function pickRetention(value: string) {
  if (!props.disabled && value !== props.retention.value) emit('save-retention', value);
}
function save() {
  if (!props.disabled) emit('save', automatic.value, props.breakpointId, interval.value, offset.value);
}
</script>

<template>
  <section class="cache-panel" aria-label="缓存断点控制">
    <header class="cache-head">
      <h2><Icon name="bolt" :size="20" />缓存</h2>
      <p>{{ status.summary }}</p>
    </header>

    <div class="cache-plate" aria-label="当前缓存接入">
      <div class="cache-plate-top">
        <span>{{ retention.role }}</span>
        <span class="cache-badge" :class="{ live: status.live }">{{ status.badge }}</span>
      </div>
      <dl>
        <dt>渠道</dt>
        <dd>{{ retention.channel }}</dd>
        <dt>接口</dt>
        <dd>
          {{ retention.apiLabel }}
          <span v-if="retention.protocolNote" class="cache-warn"
            >模型名与接口协议不一致，档位按接口匹配<FieldHelp :tooltip="retention.protocolNote"
          /></span>
        </dd>
        <dt>模型</dt>
        <dd>
          <code>{{ retention.model || '尚未配置' }}</code>
        </dd>
        <dt>缓存控制</dt>
        <dd>
          <template v-if="retention.control === 'subscription'">服务端管理</template>
          <template v-else-if="retention.control === 'implicit'"
            >服务端隐式<FieldHelp :tooltip="retention.note"
          /></template>
          <button v-else type="button" class="cache-link" @click="emit('settings')">
            {{ controlLabel }}<Icon name="arrow-right" :size="12" />
          </button>
        </dd>
      </dl>
    </div>

    <section class="cache-section" aria-label="缓存时效">
      <p v-if="retention.error" class="cache-warn" role="alert">{{ retention.error }}</p>
      <div class="cache-section-head">
        <h3>时效</h3>
        <span v-if="!retentionOff" class="cache-scope">全局 · {{ retention.role }}</span>
      </div>
      <p v-if="retentionOff" class="cache-off"><Icon name="ban" :size="14" />{{ retentionOff }}</p>
      <template v-else-if="!retention.enabled">
        <div class="cache-value"><b>服务商默认</b><small>当前不发送时效参数</small></div>
        <p class="cache-note">
          {{ retention.note }}
          <button type="button" class="cache-link" @click="emit('settings')">前往模型设置 →</button>
        </p>
      </template>
      <template v-else>
        <div
          v-if="retention.options.length === 1 && retention.options[0].value !== 'default'"
          class="cache-value"
        >
          <b>{{ retention.options[0].label }}</b
          ><small>当前模型唯一档位</small>
        </div>
        <div v-else class="panel-tabs cache-tabs" role="group" aria-label="缓存时效档位">
          <button
            v-for="option in retention.options"
            :key="option.value"
            type="button"
            :class="{ active: option.value === retention.value }"
            :aria-pressed="option.value === retention.value"
            :disabled="disabled"
            @click="pickRetention(option.value)"
          >
            {{ option.label }}
          </button>
        </div>
        <p class="cache-note">
          {{ retention.note }}{{ retention.options.length > 1 ? '点击即保存到全局设置，下次请求生效。' : '' }}
        </p>
      </template>
    </section>

    <section class="cache-section" aria-label="缓存断点">
      <div class="cache-section-head">
        <h3>
          断点<FieldHelp
            v-if="runtime"
            tooltip="选择稳定前缀的结束位置。悬停 AI 回复末尾，点击黄点可手动固定。自动跟进在保存后立即定位，之后只按成功提交的新增回合计数。同轮工具续答和重生成都不增加轮次。"
          />
        </h3>
      </div>
      <template v-if="runtime">
        <div class="panel-tabs cache-tabs" role="group" aria-label="缓存跟进方式">
          <button
            type="button"
            :class="{ active: automatic }"
            :aria-pressed="automatic"
            :disabled="disabled"
            @click="automatic = true"
          >
            自动跟进
          </button>
          <button
            type="button"
            :class="{ active: !automatic }"
            :aria-pressed="!automatic"
            :disabled="disabled"
            @click="automatic = false"
          >
            手动固定
          </button>
        </div>
        <template v-if="automatic">
          <div class="cache-sentence">
            每新增
            <span
              class="cache-stepper"
              :class="{ dirty: interval !== session.cache_breakpoint_follow?.interval }"
            >
              <button
                type="button"
                aria-label="减少跟进间隔"
                :disabled="disabled || interval <= 1"
                @click="interval--"
              >
                <Icon name="minus" :size="13" /></button
              ><output aria-label="跟进间隔">{{ interval }}</output
              ><button type="button" aria-label="增加跟进间隔" :disabled="disabled" @click="interval++">
                <Icon name="plus" :size="13" />
              </button>
            </span>
            轮跟进一次
          </div>
          <div class="cache-sentence">
            位置：倒数第
            <span
              class="cache-stepper"
              :class="{ dirty: offset !== session.cache_breakpoint_follow?.offset }"
            >
              <button
                type="button"
                aria-label="减少倒数位置"
                :disabled="disabled || offset <= 1"
                @click="offset--"
              >
                <Icon name="minus" :size="13" /></button
              ><output aria-label="倒数位置">{{ offset }}</output
              ><button type="button" aria-label="增加倒数位置" :disabled="disabled" @click="offset++">
                <Icon name="plus" :size="13" />
              </button>
            </span>
            个 AI 回复末尾
          </div>
          <p v-if="offset === 1" class="cache-warnline">
            倒数第 1 个属于激进策略：最新回复进入缓存前缀，编辑或重新生成它会使缓存失效并重新写入。
          </p>
        </template>
        <p v-else class="cache-note">位置手动固定：悬停 AI 回复末尾，点击黄点可改。</p>
        <div v-if="changed" class="cache-actions">
          <button type="button" class="quiet" :disabled="disabled" @click="reset">还原</button>
          <button type="button" class="primary" :disabled="disabled" @click="save">保存策略</button>
        </div>
      </template>
      <p v-else class="cache-off">
        <Icon name="ban" :size="14" /><span
          >{{ breakpointOff.text
          }}<button v-if="breakpointOff.link" type="button" class="cache-link" @click="emit('settings')">
            {{ breakpointOff.link }} →
          </button></span
        >
      </p>
    </section>

    <div v-if="runtime" class="cache-readout" role="status">
      <div>
        <span>计划</span>
        <strong
          >{{ plannedRef
          }}<em
            >{{ plannedIndex >= 0 ? 'AI 回复末尾 · ' : ''
            }}{{
              session.cache_breakpoint_auto
                ? `再 ${session.cache_breakpoint_follow.interval - session.cache_breakpoint_follow.progress} 轮后跟进`
                : '已手动固定'
            }}</em
          ></strong
        >
        <button class="icon-button" title="定位断点" aria-label="定位断点" @click="emit('locate')">
          <Icon name="target" :size="16" />
        </button>
      </div>
      <p v-if="session.cache_breakpoint_reason" class="cache-reason">{{ session.cache_breakpoint_reason }}</p>
      <div>
        <span>上次发送</span>
        <strong v-if="requestState?.status === 'fallback'" class="fallback"
          >已降级云端<em>{{ requestState.reason }}</em></strong
        >
        <strong v-else-if="requestState?.status === 'sent'"
          >{{ sentLabel }}<em>已发送，不代表命中</em></strong
        >
        <strong v-else class="none">尚无发送记录</strong>
      </div>
    </div>
  </section>
</template>

<style scoped>
.cache-panel {
  display: grid;
  gap: 16px;
  padding-bottom: 24px;
  font-size: var(--fs-sm);
}
.cache-head h2 {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 0;
  font-size: var(--fs-xl);
}
.cache-head h2 .icon {
  color: var(--accent);
}
.cache-head p {
  margin: 6px 0 0 29px;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
/* The connection reads like a nameplate: what model, through which door, who owns the cache. */
.cache-plate {
  overflow: hidden;
  border: 1px solid var(--glass-edge-strong);
  border-radius: var(--radius-md);
  background: linear-gradient(160deg, rgb(255 255 255 / 0.055), rgb(255 255 255 / 0.012));
}
.cache-plate-top {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px 8px 14px;
  border-bottom: 1px dashed var(--border-control);
}
.cache-plate-top > span:first-child {
  flex: 1;
  min-width: 0;
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.cache-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 20px;
  padding: 0 8px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-pill);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  white-space: nowrap;
}
.cache-badge::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.cache-badge.live {
  border-color: rgb(var(--accent-rgb) / 0.4);
  background: var(--accent-soft);
  color: var(--accent);
}
.cache-plate dl {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  margin: 0;
  padding: 4px 14px 8px;
}
.cache-plate dt,
.cache-plate dd {
  padding: 6px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.cache-plate dl > :nth-last-child(-n + 2) {
  border-bottom: 0;
}
.cache-plate dt {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.cache-plate dd {
  margin: 0;
  overflow-wrap: anywhere;
  color: var(--text-primary);
}
.cache-plate code {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}
.cache-warn {
  display: flex;
  align-items: center;
  margin-top: 3px;
  color: var(--warning);
  font-size: var(--fs-xs);
}
.cache-link {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font-size: inherit;
  text-align: left;
}
.cache-link:hover:not(:disabled) {
  background: none;
  color: var(--accent-hover);
}
.cache-section {
  display: grid;
  gap: 9px;
}
.cache-section-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.cache-section-head h3 {
  display: flex;
  flex: 1;
  align-items: center;
  margin: 0;
  font-size: var(--fs-md);
}
.cache-scope {
  padding: 1px 6px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-2xs);
  color: var(--text-muted);
  font-size: var(--fs-2xs);
  white-space: nowrap;
}
.cache-off {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 0;
  padding: 9px 11px;
  border: 1px dashed var(--border-control);
  border-radius: var(--radius-sm);
  background: rgb(255 255 255 / 0.025);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.cache-off > .icon {
  flex: none;
  margin-top: 2px;
  color: var(--text-faint);
}
.cache-off .cache-link {
  display: inline;
  margin-left: 4px;
}
.cache-tabs {
  display: flex;
}
.cache-tabs button {
  flex: 1;
}
.cache-value {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 11px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: rgb(0 0 0 / 0.2);
}
.cache-value b {
  font-weight: 500;
}
.cache-value small {
  color: var(--text-faint);
  font-size: var(--fs-xs);
}
.cache-note,
.cache-warnline {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.cache-warnline {
  color: var(--warning);
}
.cache-sentence {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  color: var(--text-secondary);
}
.cache-stepper {
  display: inline-flex;
  align-items: center;
  height: var(--control-h-sm);
  overflow: hidden;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-xs);
  background: var(--surface-input);
}
.cache-stepper.dirty {
  border-color: rgb(var(--warning-rgb) / 0.5);
}
.cache-stepper button {
  display: grid;
  place-items: center;
  width: 24px;
  height: 100%;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: none;
  color: var(--text-muted);
}
.cache-stepper output {
  min-width: 20px;
  color: var(--text-strong);
  font-family: var(--font-mono);
  text-align: center;
}
.cache-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.cache-actions button {
  padding: 5px 12px;
  font-size: var(--fs-sm);
}
.cache-readout {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: rgb(0 0 0 / 0.22);
}
.cache-readout > div {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  min-height: 40px;
  padding: 4px 6px 4px 12px;
}
.cache-readout > div + div,
.cache-readout > p + div {
  border-top: 1px solid var(--border-subtle);
}
.cache-readout span {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.cache-readout strong {
  min-width: 0;
  overflow-wrap: anywhere;
  font-family: var(--font-mono);
  font-weight: 500;
}
.cache-readout em {
  margin-left: 6px;
  color: var(--text-muted);
  font-family: var(--font-ui);
  font-size: var(--fs-xs);
  font-style: normal;
}
.cache-readout .fallback {
  color: var(--warning);
  font-family: var(--font-ui);
}
.cache-readout .none {
  color: var(--text-muted);
  font-family: var(--font-ui);
  font-weight: 400;
}
.cache-readout .icon-button {
  width: var(--control-h-sm);
  height: var(--control-h-sm);
}
.cache-reason {
  margin: -4px 12px 8px 82px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.6;
}
</style>
