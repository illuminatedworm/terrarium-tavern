import JSON5 from 'json5';
import YAML from 'yaml';
export function tr(key:string,values:any={}) {return key+' '+JSON.stringify(values);}
export function useDataStore(){return {settings:{通知:{变量更新出错:true}},effective_settings:{兼容性:{更新到聊天变量:false}}};}
export function getLastValidVariable(){throw Error('MVU persistence is owned by Tavern; direct upstream storage is unavailable');}
export function parseString(text:string){try{return JSON5.parse(text);}catch{return YAML.parse(text);}}
export function isJsonPatch(value:any){return Array.isArray(value)&&value.every(v=>v&&typeof v.path==='string'&&['add','remove','replace','move','copy','test'].includes(v.op));}
