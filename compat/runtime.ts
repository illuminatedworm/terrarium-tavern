import _ from 'lodash';
import $ from 'jquery';
import { z } from 'zod';
import YAML from 'yaml';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import * as mvu from './mvu';
import { replaceVariableMacros } from './variable-macros';
import { lazyHistory, type HistorySource } from './history';
export { frameCapabilities } from './frame-capabilities';
export { mountFrame } from './frame-client';
export { installImageLoader } from './image-loader';
export { parseStoryDisplay, htmlDocuments } from './presentation';
export { snapshotMacros } from './variable-macros';
export function renderMarkdown(text: string) { return DOMPurify.sanitize(marked.parse(text, {async:false}) as string); }

type Host = (method: string, args: any) => Promise<any>;
const w = globalThis as any;
let mirror: any = {}, bridge: Host, scriptId = 'card', currentMessage: number | undefined;
let history: ReturnType<typeof lazyHistory> | undefined;
let coreLoader: (() => Promise<void>) | undefined;
let corePending: Promise<void> | undefined;
const listeners = new Map<string, Set<(...args: any[]) => any>>();
const pending: Promise<any>[] = [];
const canonical = (name: string) => name === 'mag_variable_initiailized' ? 'mag_variable_initialized' : name;
export async function emit(name: string, ...args: any[]) {
  for (const callback of [...(listeners.get(canonical(name)) || [])]) await callback(...args);
  if (mirror.serverMain && name.startsWith('mag_')) {
    const result = await bridge('event', {name:canonical(name),args});
    if (Array.isArray(result)) result.forEach((value:any,index:number)=>{
      const target=args[index];
      if(Array.isArray(target)&&Array.isArray(value))target.splice(0,target.length,...value);
      else if(target&&typeof target==='object'&&value&&typeof value==='object'){
        for(const key of Object.keys(target))delete target[key];Object.assign(target,value);
      }
    });
  }
}
export async function dispatch(name:string,args:any[]) {
  for(const callback of [...(listeners.get(canonical(name))||[])])await callback(...args);
  return args;
}
function eventOn(name: string, callback: (...args: any[]) => any) {
  name = canonical(name);
  if (!listeners.has(name)) listeners.set(name, new Set());
  listeners.get(name)!.add(callback);
  return {stop: () => listeners.get(name)?.delete(callback)};
}
function messageIndex(index: any) {
  const n = mirror.messages.length;
  if (index === undefined || index === 'latest') index = n - 1;
  if (index < 0) index = n + index;
  if (!Number.isInteger(index) || index < 0 || index >= n) throw Error('消息楼层超出范围');
  return index;
}
function scope(option: any = {type: 'chat'}) {
  const type = option.type || 'chat';
  if (type === 'message') return `message:${messageIndex(option.message_id)}`;
  if (type === 'script') return `script:${option.script_id || scriptId}`;
  if (!['chat','character','global','preset','extension'].includes(type)) throw Error('不支持的变量范围');
  return type === 'extension' ? `extension:${option.extension_id}` : type;
}
function getVariables(option?: any) {
  const key = scope(option);
  if (key === 'chat') return _.cloneDeep(mirror.state);
  if (key.startsWith('message:')) return _.cloneDeep(mirror.messages[Number(key.split(':')[1])].variables || {});
  return _.cloneDeep(mirror.scopes[key] || {});
}
function getAllVariables() {
  // Tavern Helper combines scopes by top-level assignment. A later stat_data
  // object replaces the earlier one; deep merge resurrects removed fields.
  const sources = [getVariables({type:'global'}), getVariables({type:'character'})];
  if (currentMessage === undefined) sources.push(getVariables({type:'script'}));
  sources.push(getVariables({type:'chat'}));
  if (currentMessage !== undefined) {
    if (history && !history.dirty) sources.push(history.source.variables(currentMessage));
    else sources.push(...mirror.messages.slice(0, currentMessage + 1).map((message:any) => message.variables || {}));
  }
  return _.cloneDeep(Object.assign({}, ...sources));
}
function chatMessages(messages: any[]) {
  return messages.map(message => {
    const copy = _.cloneDeep(message);
    // Older MVU HTML examples call the message variable bag `data`.
    return {...copy, data: copy.variables || {}};
  });
}
function substitudeMacros(text: string) {
  return replaceVariableMacros(text, type => getVariables({type}))
    .replace(/\{\{(user|char)}}/gi, (macro, name) => name.toLowerCase() === 'user' ? mirror.persona ?? macro : mirror.character ?? macro)
    .replace(/\$\{([^}]+)}/g, (macro, path) => String(_.get(mirror.state.stat_data, path, macro)));
}
function replaceVariables(variables: any, option?: any) {
  const key = scope(option);
  if (key === 'chat') mirror.state = _.cloneDeep(variables);
  else if (key.startsWith('message:')) {
    mirror.messages[Number(key.split(':')[1])].variables = _.cloneDeep(variables);
    if (history) history.dirty = true;
  }
  else mirror.scopes[key] = _.cloneDeep(variables);
  if (mirror.ui) pending.push(bridge('variables', {variables, option}));
}
export async function flush() { await Promise.all(pending.splice(0)); }
export function snapshot() { return _.cloneDeep(mirror); }
export async function update(data: any) { history = undefined; mirror = _.cloneDeep(data); }
// Transaction checkpoints must not materialize untouched historical records in every realm.
export function checkpoint() {
  if (!history) return {mirror: snapshot()};
  const {messages: _messages, ...rest} = mirror;
  return {mirror: _.cloneDeep(rest), loaded: _.cloneDeep([...history.loaded]), dirty: history.dirty};
}
export function restore(saved: any) {
  if (!history || !saved.loaded) { history = undefined; mirror = _.cloneDeep(saved.mirror); return; }
  history.loaded.clear();
  for (const [index, value] of saved.loaded) history.loaded.set(index, _.cloneDeep(value));
  history.dirty = saved.dirty;
  mirror = {..._.cloneDeep(saved.mirror), messages: history.messages};
}
export async function refresh(data: any, source?: HistorySource) {
  const previous = _.cloneDeep(mirror.state);
  Object.assign(mirror, _.cloneDeep(data));
  currentMessage = data.currentMessage;
  if (source) { history = lazyHistory(source); mirror.messages = history.messages; }
  await emit('tavern_state_updated', _.cloneDeep(mirror.state), previous);
}
export function transform(text: string, rules: any[], placement: number, target: 'display' | 'model', depth = 0) {
  let result = text;
  for (const rule of rules) {
    if (rule.disabled || (rule.placement?.length && !rule.placement.includes(placement))) continue;
    if (target === 'model' && rule.markdownOnly && !rule.promptOnly) continue;
    if (target === 'display' && rule.promptOnly && !rule.markdownOnly) continue;
    if (rule.minDepth != null && rule.minDepth >= 0 && depth < rule.minDepth) continue;
    if (rule.maxDepth != null && rule.maxDepth >= 0 && depth > rule.maxDepth) continue;
    const pattern = rule.findRegex || '';
    const match = pattern.match(/^\/([\s\S]*)\/([gimsuy]*)$/);
    const regex = match ? new RegExp(match[1], match[2]) : new RegExp(pattern, 'g');
    result = result.replace(regex, rule.replaceString || '');
    for (const trim of rule.trimStrings || []) result = result.split(trim).join('');
  }
  return result;
}
export function install(data: any, host: Host, options?: {history?: HistorySource; loadCore?: () => Promise<void>}) {
  mirror = _.cloneDeep(data); bridge = host;
  history = options?.history ? lazyHistory(options.history) : undefined;
  if (history) mirror.messages = history.messages;
  coreLoader = options?.loadCore; corePending = undefined;
  currentMessage = data.currentMessage;
  mirror.messages ||= []; mirror.state ||= {}; mirror.scopes ||= {};
  const notify = (level: string) => (...args: any[]) => pending.push(bridge('log', {level, message: args.map(String).join(' ')}));
  const api: any = {
    _, $, jQuery: $, z, YAML, klona: _.cloneDeep,
    errorCatched: (fn:any) => function(this:any,...args:any[]) {try{const result=fn.apply(this,args);if(result&&typeof result.then==='function')return result.catch((error:any)=>{notify('error')(error?.message||error);throw error;});return result;}catch(error:any){notify('error')(error?.message||error);throw error;}},
    getAllVariables, substitudeMacros,
    eventOn, eventOff: (name: string, cb: any) => listeners.get(canonical(name))?.delete(cb),
    eventOnce: (name: string, cb: any) => { const listener = eventOn(name, async (...args) => {listener.stop(); return cb(...args);}); return listener; },
    eventEmit: emit, eventEmitAndWait: emit, getScriptId: () => scriptId,
    getCurrentMessageId: () => currentMessage ?? mirror.messages.length - 1,
    getLastMessageId: () => mirror.messages.length - 1,
    getVariables, replaceVariables,
    updateVariablesWith: (fn: any, option: any) => {const out = fn(getVariables(option)); if (out instanceof Promise) return out.then(v => {replaceVariables(v, option); return v;}); replaceVariables(out, option); return out;},
    insertOrAssignVariables: (value: any, option: any) => {const out = _.mergeWith(getVariables(option), value, (_old:any,next:any)=>Array.isArray(next)?_.cloneDeep(next):undefined); replaceVariables(out,option); return out;},
    insertVariables: (value: any, option: any) => {const out = _.mergeWith({}, value, getVariables(option), (_old:any,next:any)=>Array.isArray(next)?_.cloneDeep(next):undefined); replaceVariables(out,option); return out;},
    deleteVariable: (path: string, option: any) => {const out = getVariables(option); const deleted = _.has(out,path); _.unset(out,mvu.safePath(path)); replaceVariables(out,option); return {variables: out, delete_occurred: deleted};},
    registerVariableSchema: (schema: any, option: any) => { mirror.schemas ||= {}; mirror.schemas[scope(option)] = z.toJSONSchema(schema,{unrepresentable:'any'}); },
    getChatMessages: (range: any = '0-{{lastMessageId}}') => {
      if (typeof range === 'number' || range === 'latest') return chatMessages([mirror.messages[messageIndex(range)]]);
      const found = String(range).match(/^(-?\d+)(?:-(\d+|\{\{lastMessageId}}))?$/);
      if (!found) throw Error('不支持的消息范围');
      const start = messageIndex(Number(found[1]));
      const end = found[2] ? messageIndex(found[2].startsWith('{') ? 'latest' : Number(found[2])) : start;
      return chatMessages(mirror.messages.slice(start,end+1));
    },
    setChatMessages: async (messages: any[]) => {for (const m of messages) Object.assign(mirror.messages[messageIndex(m.message_id)], m); if (history) history.dirty = true; if (mirror.ui) await bridge('messages', {messages});},
    getWorldbookNames: () => [mirror.bookName || 'character'],
    getCharWorldbookNames: () => ({primary: mirror.bookName || 'character', additional: []}),
    getWorldbook: async (_name: string) => _.cloneDeep(mirror.lore || []),
    replaceWorldbook: async (_name: string, entries: any[]) => {mirror.lore = _.cloneDeep(entries); mirror.loreModified = true; if (mirror.ui) await bridge('lore', {entries});},
    getLorebookEntries: async (_name: string) => _.cloneDeep(mirror.lore || []),
    setLorebookEntries: async (_name: string, entries: any[]) => {mirror.lore = _.cloneDeep(entries); mirror.loreModified = true;},
    generate: async (config: any = {}) => generation(config),
    generateRaw: async (config: any = {}) => generation(config),
    stopGenerationById: (id: string) => {pending.push(bridge('cancel', {id})); return true;},
    stopAllGeneration: () => {pending.push(bridge('cancel', {})); return true;},
    waitGlobalInitialized: async (name: string) => {if (!(name in w)) throw Error(`缺少全局依赖：${name}`);},
    getButtonEvent: (name: string) => `button:${name}`,
    replaceScriptButtons: (buttons: any[]) => {mirror.buttons = buttons;},
    getScriptButtons: () => _.cloneDeep(mirror.buttons || []),
    toastr: {info: notify('info'), warning: notify('warning'), error: notify('error'), success: notify('success')},
    iframe_events: {GENERATION_REQUESTED:'generation_requested', GENERATION_STARTED:'generation_started', GENERATION_ENDED:'generation_ended', STREAM_TOKEN_RECEIVED_FULLY:'stream_token_received_fully', STREAM_TOKEN_RECEIVED_INCREMENTALLY:'stream_token_received_incrementally'},
    tavern_events: {CHAT_CHANGED:'CHAT_CHANGED', MESSAGE_RECEIVED:'MESSAGE_RECEIVED', MESSAGE_UPDATED:'MESSAGE_UPDATED', GENERATION_STARTED:'generation_started', GENERATION_ENDED:'generation_ended'},
  };
  async function generation(config: any) {
    if (config.custom_api || config.preset_name && config.preset_name !== 'in_use') throw Error('脚本生成仅支持当前模型配置；不支持 ST 代理或预设');
    await emit('generation_requested', config); await emit('generation_started', config);
    const result = await bridge('generate', config);
    if(!mirror.ui || !config.should_stream){
      await emit('stream_token_received_fully', result);
      await emit('stream_token_received_incrementally', result);
    }
    await emit('generation_ended', result);
    return result;
  }
  api.Mvu = {events: {...mvu.events,...w.MVUReference?.variable_events}, getMvuData: getVariables, replaceMvuData: async (data: any, option: any) => replaceVariables(data,option),
    parseMessage: async (text: string, old: any) => (await applyMvu(text,old)).data, isDuringExtraAnalysis: () => false};
  Object.assign(w, api);
  w.TavernHelper = api;
  return api;
}
export async function runScripts(scripts: any[]) {
  for (const script of scripts) {
    scriptId = script.id || script.name || 'card';
    const content = script.content || '';
    if (!script.bundled && /\bimport\s*(?:\(|[{'"*])/.test(content)) throw Error(`脚本 ${scriptId} 依赖外部模块；请先解析依赖`);
    // Executed only inside a browser sandbox without host credentials or network.
    await new Function(`return (async () => {${content}\n})()`)();
  }
  await new Promise<void>(resolve => $(()=>resolve()));
  await flush();
}
export async function initMvu(greeting = '') {
  mirror.state = await mvu.initialize(mirror.lore || [],mirror.state,emit,greeting);
  if (mirror.messages.length) mirror.messages.at(-1).variables = _.cloneDeep(mirror.state);
  return snapshot();
}
export async function process(text: string, extra: any[] = [], variableText?: string) {
  // Separate background updates from narrative; omitted argument retains legacy SDK behavior.
  const result = variableText === '' && !extra.length
    ? {text, data: _.cloneDeep(mirror.state)}
    : await applyMvu(variableText ?? text,mirror.state,extra);
  mirror.state = result.data;
  if (mirror.messages.length) mirror.messages.at(-1).variables = _.cloneDeep(result.data);
  await emit('MESSAGE_RECEIVED', mirror.messages.length-1);
  await flush();
  return {text: variableText === undefined ? result.text : text, ...snapshot()};
}

async function applyMvu(text:string,old:any,extra:any[]=[]):Promise<{text:string,data:any}> {
  if (!w.MVUReference && coreLoader) {
    corePending ||= coreLoader().catch(error => { corePending = undefined; throw error; });
    await corePending;
    if (!w.MVUReference) throw Error('MVU 核心未加载');
  }
  if (!w.MVUReference) return mvu.apply(text,old,emit,extra);
  const data=_.cloneDeep(old);
  data.stat_data ||= {};
  // Fail closed on prohibited paths before entering upstream; callbacks may repair values.
  for(const command of mvu.commands(text)) {
    const path=command.args[0]?.replace(/^['"]|['"]$/g,'');
    if(path)mvu.safePath(path);
  }
  if (extra.length) await mvu.apply('',old,async()=>{},extra);
  const previousWarning=w.toastr.warning;
  w.toastr.warning=(...args:any[])=>{previousWarning(...args);throw Error(`MVU_UPDATE_INVALID: ${args.map(String).join(' ')}`);};
  try {
    const suffix=extra.length?`\n<JSONPatch>${JSON.stringify(extra)}</JSONPatch>`:'';
    await w.MVUReference.updateVariables(text+suffix,data);
    const context={variables:data,message_content:text};
    await emit(mvu.events.BEFORE_MESSAGE_UPDATE,context);
    return {data:context.variables,text:context.message_content};
  } finally {w.toastr.warning=previousWarning;}
}
