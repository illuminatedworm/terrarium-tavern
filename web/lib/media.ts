import type { InjectionKey, Ref } from 'vue';

export interface MediaConnection {
  enabled: boolean;
  provider_id: string;
  api_path: string;
  model: string;
  adapter: string;
  timeout_seconds: number;
  quality?: string;
  instructions?: string;
}
export interface MediaConfig {
  speech: MediaConnection;
  image: MediaConnection;
  view_images: boolean;
  max_images: number;
  max_speech_segments: number;
  max_speech_characters: number;
  voices: Record<string, string>;
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
