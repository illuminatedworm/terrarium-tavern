import type { InjectionKey, Ref } from 'vue';
import type { Channel } from './channels';

export type MediaKind = 'speech' | 'image';
export interface ModelKey {
  provider_id: string;
  model: string;
}
interface BaseConnection extends ModelKey {
  api_path: string;
  timeout_seconds: number;
}
export type SpeechProfile = BaseConnection & { instructions: string; default_voice: string } & (
    | {
        adapter: 'openai_speech';
        speed: number;
        send_instructions: boolean;
        voice_names: Record<string, string>;
      }
    | { adapter: 'openai_realtime' }
    | {
        adapter: 'gemini_tts';
        temperature: number | null;
        language_code: string;
        instruction_format: 'auto' | 'speech_metadata' | 'prompt';
      }
    | { adapter: 'seed_tts'; speech_rate: number; loudness_rate: number; sample_rate: number }
  );
export type ImageProfile = BaseConnection &
  (
    | { adapter: 'openai_images'; quality: string; size: string; output_format: string }
    | { adapter: 'gemini_image'; resolution: string; aspect_ratio: string }
    | { adapter: 'ark_seedream'; size: string; output_format: string; watermark: boolean }
  );
export type SpeechConnection = SpeechProfile & { enabled: boolean };
export type ImageConnection = ImageProfile & { enabled: boolean };
export type MediaConnection = SpeechConnection | ImageConnection;
export type Profile = SpeechProfile | ImageProfile;
export type Adapter = Profile['adapter'];
export const adapterLabels: Record<Adapter, string> = {
  openai_speech: 'Speech HTTP · /audio/speech',
  openai_realtime: 'Realtime · WebSocket',
  gemini_tts: 'Gemini · generateContent',
  seed_tts: '豆包语音 · 单向流式合成',
  openai_images: 'Images HTTP · /images/generations',
  gemini_image: 'Gemini · generateContent',
  ark_seedream: 'Seedream · 方舟图像生成',
};
export interface Channels<P extends Profile> {
  active_channel: Channel;
  standard: P;
  chatgpt: P;
  remembered: P[];
}
export type SpeechChannels = Channels<SpeechProfile> & { max_segments: number; max_characters: number };
export type ImageChannels = Channels<ImageProfile> & { max_images: number };
export interface MediaDefaults {
  speech: SpeechChannels;
  image: ImageChannels;
}
export interface MediaConfig {
  speech: SpeechConnection;
  image: ImageConnection;
  max_images: number;
  max_speech_segments: number;
  max_speech_characters: number;
  view_images: boolean;
  voices: Record<string, string>;
}
export interface VoiceTable {
  story_model: ModelKey;
  speech_model: ModelKey;
  voices: Record<string, string>;
}
export interface MediaSelection {
  speech: { enabled: boolean; custom: SpeechChannels | null };
  image: { enabled: boolean; custom: ImageChannels | null };
  view_images: boolean;
  voice_tables: VoiceTable[];
}
export interface MediaProtocols {
  presets: MediaDefaults;
  keyless_adapters: Record<string, Adapter[]>;
  speech_voices: Record<SpeechProfile['adapter'], string[]>;
  seed_voice_names: Record<string, string>;
  gemini_speech: { prompt_prefixes: string[]; metadata_voices: string[] };
  adapters: { [A in Adapter]: Extract<MediaConnection, { adapter: A }> };
}
export type MediaSource = 'story' | 'global' | 'mixed';
export const modelKey = ({ provider_id, model }: ModelKey): ModelKey => ({ provider_id, model });
export const sameModel = (a: ModelKey, b: ModelKey) => a.provider_id === b.provider_id && a.model === b.model;
export function metadataStyle(speech: SpeechProfile, catalog: MediaProtocols) {
  if (speech.adapter !== 'gemini_tts') return false;
  if (speech.instruction_format !== 'auto') return speech.instruction_format === 'speech_metadata';
  const name = speech.model.replace(/^models\//, '');
  return !catalog.gemini_speech.prompt_prefixes.some((prefix) => name.startsWith(prefix));
}
export function voiceOptions(p: SpeechProfile, catalog: MediaProtocols) {
  const ids = metadataStyle(p, catalog)
    ? catalog.gemini_speech.metadata_voices
    : catalog.speech_voices[p.adapter];
  const names =
    p.adapter === 'openai_speech' ? p.voice_names : p.adapter === 'seed_tts' ? catalog.seed_voice_names : {};
  return [...new Set([...Object.keys(names), ...ids])].map((id) => ({
    value: id,
    label: names[id] || id,
    description: names[id] ? id : undefined,
  }));
}
export interface MediaAsset {
  id: string;
  session: string;
  message: string | null;
  ref: string;
  kind: 'speech' | 'image';
  name: string;
  description: string;
  mime: string;
  size: number;
  selected: boolean | number;
  created: number;
}
export interface MediaJob {
  id: string;
  session: string;
  message: string;
  kind: 'speech' | 'image';
  status: 'queued' | 'running' | 'completed' | 'failed' | 'interrupted' | 'cancelled';
  request: {
    connection: MediaConnection & { provider_name: string; base_url: string };
    text?: string;
    speaker?: string;
    voice?: string;
    delivery?: string;
    prompt?: string;
    size?: string;
    reference_names?: Array<{ ref: string; name: string }>;
  };
  previous: string | null;
  output: MediaAsset | null;
  error: string;
  created: number;
  seq: number;
  ordinal: number;
}
export interface StoryMedia {
  jobs: Ref<Record<string, MediaJob[]>>;
  error: Ref<string>;
  register: (message: string) => () => void;
  reload: () => void;
  playing: (audio: HTMLAudioElement) => void;
  action: (job: MediaJob, action: 'retry' | 'regenerate' | 'cancel') => Promise<void>;
}
export const storyMediaKey: InjectionKey<StoryMedia> = Symbol('story-media');
export const mediaUrl = (asset: MediaAsset) => `/api/sessions/${asset.session}/media/assets/${asset.id}`;
export const mediaStatus: Record<MediaJob['status'], string> = {
  queued: '等待生成',
  running: '正在生成',
  completed: '已完成',
  failed: '生成失败',
  interrupted: '已中断，未自动重试',
  cancelled: '已取消',
};
