<script setup lang="ts">
import FieldHelp from '../common/FieldHelp.vue';
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { maintenanceLimit, settingsErrors } from '../../lib/settingsValidation';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import ModelSettings from './ModelSettings.vue';
import RetrievalSettings from './RetrievalSettings.vue';
import PolicySelect from './PolicySelect.vue';
import RecipeEditor from './RecipeEditor.vue';
import StorageSettings from './StorageSettings.vue';
import FullAgentSettings from './FullAgentSettings.vue';
const props = defineProps<{
  settings: any;
  saving?: boolean;
  effortOptions: Array<{ value: string; label: string }>;
}>();
const emit = defineEmits<{ save: []; probe: [] }>();
const page = ref('model');
const pagesNav = ref<HTMLElement | null>(null);
function revealCurrentPage() {
  const nav = pagesNav.value;
  if (!nav || nav.scrollWidth <= nav.clientWidth) return;
  const active = nav.querySelector<HTMLElement>('[aria-current="page"]');
  if (!active) return;
  const navBounds = nav.getBoundingClientRect();
  const activeBounds = active.getBoundingClientRect();
  if (activeBounds.left < navBounds.left + 8) {
    nav.scrollLeft -= navBounds.left + 8 - activeBounds.left;
  } else if (activeBounds.right > navBounds.right - 8) {
    nav.scrollLeft += activeBounds.right - navBounds.right + 8;
  }
}
watch(page, async () => {
  await nextTick();
  revealCurrentPage();
});
onMounted(() => window.addEventListener('resize', revealCurrentPage));
onUnmounted(() => window.removeEventListener('resize', revealCurrentPage));
function promptOwner(path: string) {
  const parts = path.split('.');
  return parts.length === 1 ? props.settings : props.settings[parts[0]];
}
function promptKey(path: string) {
  return path.split('.').at(-1)!;
}
watch(
  () => props.settings.foreground,
  (p) => {
    if (p)
      Object.assign(props.settings, {
        model: p.model,
        context_tokens: p.context_tokens,
        reply_tokens: p.reply_tokens,
        tokenizer: p.tokenizer,
        reasoning_effort: p.reasoning_effort,
      });
  },
  { deep: true },
);
const errors = computed(() => settingsErrors(props.settings));
const periodLimit = computed(() => maintenanceLimit(props.settings));
const removalLimit = computed(() => 100);
const inputBudget = computed(() => props.settings.context_tokens - props.settings.reply_tokens - 2048);
</script>

<template>
  <header class="settings-header">
    <h2>设置</h2>
  </header>
  <div class="settings-layout">
    <nav ref="pagesNav" class="settings-pages" aria-label="设置页面">
      <button
        :class="{ active: page === 'full_agent' }"
        :aria-current="page === 'full_agent' ? 'page' : undefined"
        @click="page = 'full_agent'"
      >
        Full Agent
      </button>
      <button
        :class="{ active: page === 'model' }"
        :aria-current="page === 'model' ? 'page' : undefined"
        @click="page = 'model'"
      >
        模型与偏好
      </button>
      <button
        @click="page = 'retrieval'"
        :class="{ active: page === 'retrieval' }"
        :aria-current="page === 'retrieval' ? 'page' : undefined"
      >
        混合召回与本地模型
      </button>
      <button
        @click="page = 'storage'"
        :class="{ active: page === 'storage' }"
        :aria-current="page === 'storage' ? 'page' : undefined"
      >
        存储与维护
      </button>
      <button
        @click="page = 'network'"
        :class="{ active: page === 'network' }"
        :aria-current="page === 'network' ? 'page' : undefined"
      >
        网络与代理
      </button>
      <button
        :class="{ active: page === 'lore' }"
        :aria-current="page === 'lore' ? 'page' : undefined"
        @click="page = 'lore'"
      >
        世界书注入
      </button>
      <button
        :class="{ active: page === 'maintenance' }"
        :aria-current="page === 'maintenance' ? 'page' : undefined"
        @click="page = 'maintenance'"
      >
        Half Agent · 历史与维护
      </button>
      <button
        :class="{ active: page === 'prompt' }"
        :aria-current="page === 'prompt' ? 'page' : undefined"
        @click="page = 'prompt'"
      >
        系统提示词
      </button>
      <button
        :class="{ active: page === 'context' }"
        :aria-current="page === 'context' ? 'page' : undefined"
        @click="page = 'context'"
      >
        上下文结构编排
      </button>
    </nav>
    <div class="settings-detail">
      <section v-if="page === 'model'" aria-labelledby="model-settings-title">
        <h3 id="model-settings-title">模型与偏好</h3>
        <ModelSettings :settings="settings" :effort-options="effortOptions" />
        <PolicySelect v-model="settings.policy" global />
      </section>
      <section v-else-if="page === 'retrieval'"><RetrievalSettings :settings="settings" /></section>
      <FullAgentSettings v-else-if="page === 'full_agent'" :settings="settings" />
      <section v-else-if="page === 'storage'"><StorageSettings :settings="settings" /></section>
      <section v-else-if="page === 'network'">
        <h3>网络与代理</h3>
        <label>代理地址<input v-model="settings.network.proxy" /></label
        ><label
          ><input type="checkbox" v-model="settings.network.api_proxy" />API
          请求使用代理（各提供商可单独关闭）</label
        ><label><input type="checkbox" v-model="settings.network.hf_proxy" />Hugging Face 下载使用代理</label
        ><label
          ><input type="checkbox" v-model="settings.network.resources_proxy" />角色外部资源使用代理</label
        >
      </section>
      <section v-else-if="page === 'lore'" aria-labelledby="lore-settings-title">
        <h3 id="lore-settings-title">世界书注入</h3>
        <div class="form-row">
          <label
            >单次新增条目上限<input
              type="number"
              min="1"
              max="1000"
              v-model.number="settings.lore_max_entries" /></label
          ><label
            >单次新增字符预算（字符）<input
              type="number"
              min="1"
              max="2000000"
              v-model.number="settings.lore_max_chars"
          /></label>
        </div>
        <p class="muted">
          每次最多新增指定数量的非常驻条目，正文合计不超过字符预算（默认 5 条、3000
          字符）。按变量替换后的正文计算，中文、英文、空格和换行均按 Unicode 码点计为字符，不是
          tokens。常驻条目和已经保留的滚动条目不占本次额度；超限条目整条跳过。所有内容仍受总模型上下文窗口约束，修改适用于已有对话的后续请求。
        </p>
        <div class="form-row">
          <label><input type="checkbox" v-model="settings.lore_recursive" />世界书递归查询</label
          ><label
            >递归深度<input
              type="number"
              min="0"
              max="16"
              :disabled="!settings.lore_recursive"
              v-model.number="settings.lore_recursion_depth"
          /></label>
        </div>
        <p class="muted">
          默认一层。仅本轮直接准入条目可触发递归，旧注入不继续扩展。递归候选仍须相对原始查询通过准入，已被拒绝的条目不会再次进入。关键词证据、BM25
          相对分数和重排分数均不是相关概率；额度是上限，不以弱匹配补满。
        </p>
        <label
          >新对话的注入方案
          <FieldHelp tooltip="全局选择只影响之后新建的对话。已有对话保持原方案，继承进度时复制原对话方案。" />
          <select v-model="settings.lore_injection_mode">
            <option value="conversation">随对话滚动注入（默认）</option>
            <option value="st_top">ST 式顶部可变注入</option>
          </select>
        </label>
        <p>
          随对话滚动：非常驻条目作为中途系统提示词，紧贴最新用户消息前注入。第一次发言若有开场白，则紧贴开场白前注入。此后内容和位置保持不变，与对应历史消息一起退出模型请求。
        </p>
        <p>
          重复命中不会再次注入或移动。只有条目离开正文请求后，才开始冷却，退出的那次请求不会立即再次注入；没有“持续轮数”。常驻条目每次按角色卡与上下文配方的位置注入，不受冷却影响。
        </p>
        <p>
          ST
          式：每次根据当前上下文重新选择条目，按角色卡与配方的位置排列，顶部内容可以随本轮命中变化。仍从条目离开正文请求时开始冷却。
        </p>
        <p class="muted">
          关键词扫描当前窗口中的检索线索与最新输入，扫描深度继续生效。滚动方案忽略非常驻条目的位置、深度和角色字段，统一使用
          system；这些字段保留供常驻和 ST 方案使用。
        </p>
        <p class="muted">
          冷却采用概率恢复：经过条目“恢复尺度”指定的轮数恢复至
          50%，四倍轮数完全恢复；最新输入直接提及会提高机会。预览和 Tracker 记忆整理不会推进冷却。中途 system
          原位发送，接口不支持时显示实际错误。
        </p>
      </section>
      <section v-else-if="page === 'maintenance'" aria-labelledby="maintenance-settings-title">
        <h3 id="maintenance-settings-title">历史窗口与后台维护</h3>
        <p>
          一轮是一次已提交的用户发言及其回复，通常对应两条消息。开场白算一条；中途世界书不计入消息 cap
          或每批移除量，但仍占用 token 预算。重新生成、预览和手动整理不增加轮数。
        </p>
        <label
          >窗口模式
          <FieldHelp
            tooltip="前两种模式都受消息 cap 和总上下文压力约束。历史预算模式额外检查历史 token 阈值。关闭后保留全部原文，周期维护仍执行；超过请求预算会明确报错。"
          />
          <select v-model="settings.chunk_mode">
            <option value="messages">按消息批次</option>
            <option value="tokens">按历史预算</option>
            <option value="off">关闭（保留全部原文）</option>
          </select>
        </label>
        <div class="form-row">
          <label
            >历史消息 cap
            <FieldHelp
              tooltip="仅计算用户和 AI 的历史消息及本次用户输入，开场白算一条，不计中途世界书条目。达到 cap 时，先整理待移除内容，再整批推进窗口。原始存档始终保留。" /><input
              type="number"
              min="8"
              max="1000"
              :disabled="settings.chunk_mode === 'off'"
              v-model.number="settings.history_message_cap"
          /></label>
          <label
            >每批移除量
            <FieldHelp
              tooltip="仅按用户和 AI 消息计数，不计中途世界书条目；达到批次目标后对齐完整交流边界。消息退出时，紧贴其前的全部世界书条目一同退出。保留最近两次完整交流及其条目和新输入。" /><input
              type="number"
              min="2"
              :max="removalLimit"
              step="1"
              :disabled="settings.chunk_mode === 'off'"
              v-model.number="settings.chunk_size"
          /></label>
        </div>
        <div class="form-row">
          <label
            >记忆周期（轮）
            <FieldHelp
              tooltip="每 1–100 轮整理一次已提交的新剧情，按分类增量保存发展、事项及来源。不会修改设定或 MVU；空操作可表示无新增发展。编辑旧剧情仅提示，需手动重建。" /><input
              type="number"
              min="1"
              :max="Math.max(1, periodLimit)"
              v-model.number="settings.memory_interval_turns"
          /></label>
        </div>
        <p class="muted">
          窗口 cap 和每批移除量均只计算用户和 AI 消息（cap
          包含本次输入），实际退出量对齐完整交流，退出消息前的全部世界书条目随之移除。记忆周期独立为 1–100
          轮；退出前始终验证来源覆盖。关闭窗口保留全部原文。
        </p>
        <label
          >日常上下文预算（tokens）<input
            type="number"
            min="2048"
            max="2000000"
            v-model.number="settings.daily_context_budget"
        /></label>
        <p class="muted">
          默认 98,304 tokens，与模型可用输入容量取较小值。使用前台 Writer
          选定的分词器计算，压力触发和目标也使用同一 token 口径。实际用量以服务商报告为准。
        </p>
        <div class="form-row">
          <label
            >历史预算（tokens）
            <FieldHelp
              tooltip="仅历史预算模式使用。不得超过模型上下文窗口减单次输出上限及 2048 tokens 安全余量。本地使用所选 BPE 分词器计数，服务商专用分词器不同可能产生差异。" /><input
              type="number"
              min="512"
              :max="Math.max(512, inputBudget)"
              :disabled="settings.chunk_mode !== 'tokens'"
              v-model.number="settings.chunk_max_tokens"
          /></label>
          <label
            >记忆整理输出上限（tokens）
            <FieldHelp
              tooltip="仅控制周期、裁剪前及手动记忆整理，范围 256–65536 tokens，新安装默认 4096；旧配置继承原后台上限。包含 JSON 操作与来源，不影响 MVU 和设定维护。增大可能增加费用及等待。" /><input
              type="number"
              min="256"
              max="65536"
              v-model.number="settings.memory_output_tokens"
          /></label>
        </div>
        <div class="form-row">
          <label
            >整理输入上限（tokens）<FieldHelp
              tooltip="默认32768，范围2048–2000000。包括已有记忆、完整交流、前文、参考设定及协议；还受后台模型容量减输出和2048安全余量约束。超限按完整交流分批，不截断原文。" /><input
              type="number"
              min="2048"
              max="2000000"
              v-model.number="settings.memory_input_tokens"
          /></label>
          <label
            >辅助资料预算（tokens）<FieldHelp
              tooltip="默认4096，范围0–32768，必须小于输入上限且包含在其中。优先当时注入的世界书，再用本地文本搜索补充；0关闭辅助资料。资料不足会保留不确定结论，不启动GPU模型。" /><input
              type="number"
              min="0"
              max="32768"
              v-model.number="settings.memory_evidence_tokens"
          /></label>
        </div>
        <label
          >活跃记忆容量（tokens）<FieldHelp
            tooltip="默认4096，范围512–65536。超过容量时按来源时间从旧到新退出自动生成的经历；仍超限则保留其他分类并提示。增大也会增加之后Writer与Tracker的输入。" /><input
            type="number"
            min="512"
            max="65536"
            v-model.number="settings.memory_capacity_tokens"
        /></label>
        <div class="form-row">
          <label
            >当前进展条数上限<FieldHelp
              tooltip="默认5条，范围1–100。只计算自动生成的当前进展，超出时按最新剧情来源从旧到新退出；手工条目不占额度。" /><input
              type="number"
              min="1"
              max="100"
              v-model.number="settings.memory_current_max_records"
          /></label>
          <label
            >经历冷却轮数<FieldHelp
              tooltip="默认20轮，范围0–10000；0关闭按轮数退出。自经历最新引用的已完成剧情轮次起算。达到轮数后自动退出，容量超限时也会先退出最旧经历。手工条目不参与。" /><input
              type="number"
              min="0"
              max="10000"
              v-model.number="settings.memory_episode_cooldown_rounds"
          /></label>
        </div>
        <label class="check"
          ><input type="checkbox" v-model="settings.memory_review_enabled" />允许一次疑难复核</label
        >
        <p class="muted">
          常规整理一次请求。只有关键变化待核对且找到新材料时，才追加最多一次复核（输入最多8192、输出最多2048
          tokens）。失败或无新证据时保留待确认，不自动重试。自动整理一轮最多两次分批请求加一次复核；裁剪仍未获覆盖时暂停发送。手动重建可处理更多批次，启动前显示估算并支持取消。
        </p>
        <p class="muted">
          来源由程序预先编号：模型只选择原文片段 S/H、旧记忆 R 和设定 E 的短编号，不必复制长 ID、哈希或原话。
          真实消息引用、逐字摘录及指纹由程序回填，新条目永久 ID
          由程序生成。编号仅本次请求有效；引用不存在的编号、
          只用旧背景证明新变化等情况仍会拒绝保存，并保留旧记忆。已有存档无需重建或额外请求即可继续使用。
        </p>
        <p class="muted">
          Tracker 可按需搜索或读取旧剧情，作为分类记忆之外的补充。历史工具每次完整维护固定最多 3
          次：搜索、读取、无结果及参数错误均计次，分批、复核和本轮授权维护共享额度；手动重建整次也只有 3
          次。工具返回最多约 2048 tokens，超长原文会标明片段不完整。
          本地查询本身不调用模型，但使用工具后需要模型续答，自动记忆整理因此最多增加 3
          次请求，计入各次输入、输出与超时限制。不使用工具时不增加请求。后台模型需支持原生函数调用；Writer
          保持零工具，MVU 不变。
        </p>
        <label
          >每次维护超时（秒）
          <FieldHelp
            tooltip="每次模型整理请求的等待上限，范围 5–180 秒。较长原文可能分成多个请求，每个请求单独计时；失败不会自动重试。" /><input
            type="number"
            min="5"
            max="180"
            v-model.number="settings.memory_timeout"
        /></label>
        <div class="form-row">
          <label
            >压力触发阈值（%）
            <FieldHelp
              tooltip="开启窗口时，完整请求估算达到日常输入 token 预算的该比例，就提前整理并整批裁剪，即使还没达到消息 cap。范围 10%–99%。" /><input
              type="number"
              min="10"
              max="99"
              :disabled="settings.chunk_mode === 'off'"
              :value="Number(settings.context_pressure_trigger) * 100"
              @input="
                settings.context_pressure_trigger = Number(($event.target as HTMLInputElement).value) / 100
              "
          /></label>
          <label
            >压力裁剪目标（%）
            <FieldHelp
              tooltip="压力触发后，尽量整批裁剪至此比例；必须低于触发阈值，范围 5%–98%。最近两次交流及本次输入仍受保护，无法满足预算时终止请求。" /><input
              type="number"
              min="5"
              max="98"
              :disabled="settings.chunk_mode === 'off'"
              :value="Number(settings.context_pressure_target) * 100"
              @input="
                settings.context_pressure_target = Number(($event.target as HTMLInputElement).value) / 100
              "
          /></label>
        </div>
        <p class="muted">
          周期整理失败会保留旧档案。必须裁剪时，记忆成功覆盖待移除消息后才推进窗口；失败则保留原窗口并停止本次发送。所有变更随正文一起提交，取消或拒绝审核不会推进窗口。
        </p>
      </section>
      <section v-else-if="page === 'prompt'" aria-labelledby="system-prompt-title">
        <h3 id="system-prompt-title">系统提示词</h3>
        <p class="muted">管理应用内全部系统提示词。修改或恢复默认值后，点击“保存设置”生效。</p>
        <div v-for="prompt in settings.prompt_definitions || []" :key="prompt.path" class="prompt-setting">
          <label>
            {{ prompt.label }}
            <ExpandableTextarea
              v-model="promptOwner(prompt.path)[promptKey(prompt.path)]"
              :label="prompt.label"
            />
          </label>
          <p class="muted">{{ prompt.description }}</p>
          <button
            type="button"
            :aria-label="'恢复默认值：' + prompt.label"
            @click="promptOwner(prompt.path)[promptKey(prompt.path)] = prompt.default"
          >
            恢复默认值
          </button>
        </div>
      </section>
      <section v-show="page === 'context'" aria-label="全局上下文结构编排">
        <h3>全局上下文结构编排</h3>
        <p class="muted">
          新对话默认复制此编排。每个对话可单独自定义；继承剧情进度时复制原对话的编排。修改全局编排不覆盖已有对话。
        </p>
        <RecipeEditor
          v-if="settings.context_recipe"
          :recipe="settings.context_recipe"
          :saving="saving || !!errors.length"
          save-label="保存全局编排"
          @save="
            (recipe) => {
              settings.context_recipe = recipe;
              emit('save');
            }
          "
        />
      </section>
      <AppNotification :message="errors.length ? '设置之间存在冲突：' + errors.join('；') : ''" />
      <div v-if="page !== 'context'" class="modal-footer">
        <button class="primary" :disabled="saving || !!errors.length" @click="emit('save')">
          {{ saving ? '正在保存…' : '保存设置' }}
        </button>
      </div>
    </div>
  </div>
</template>
