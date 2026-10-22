import type { CardDefaults } from './types';

export type TransferItem = {
  id: string;
  name: string;
  agent_mode?: string;
  messages?: number;
  /** Catalog cards only: the new-conversation defaults exported beside the card. */
  defaults?: CardDefaults;
};
export type TransferCatalog = Record<'personas' | 'worldbooks' | 'cards' | 'sessions', TransferItem[]>;
export type TransferPreview = TransferCatalog & {
  settings: boolean;
  includes_api_keys: boolean;
  created_at: string;
  sha256: string;
  settings_revisions: Record<string, number>;
};
export type TransferResult = {
  personas: string[];
  worldbooks: string[];
  cards: string[];
  sessions: string[];
  settings_restored: boolean;
  browser_preferences: { layout?: Record<string, unknown> };
};

export type ReadingLayout = ReturnType<typeof defaultReadingLayout>;

/** Matches .inspector-panel's CSS cap so the stored width never exceeds what is shown. */
export function clampRightWidth(width: number) {
  return Math.min(640, Math.floor(window.innerWidth * 0.35), Math.max(240, width));
}

export function defaultReadingLayout(width: number) {
  return {
    fontSize: 18,
    lineHeight: 1.9,
    readingWidth: 1120,
    fontFamily: 'sans',
    leftCollapsed: width < 760,
    rightCollapsed: width < 760,
    rightWidth: 380,
    glass: true,
  };
}

export function readBrowserPreferences() {
  const defaults = defaultReadingLayout(window.innerWidth);
  try {
    const layout = { ...defaults, ...JSON.parse(localStorage.getItem('tavern-layout') || '{}') };
    const keys = [
      'fontSize',
      'lineHeight',
      'readingWidth',
      'fontFamily',
      'leftCollapsed',
      'rightCollapsed',
      'rightWidth',
      'glass',
    ];
    return {
      layout: Object.fromEntries(keys.filter((key) => key in layout).map((key) => [key, layout[key]])),
    };
  } catch {
    return { layout: defaults };
  }
}

export async function downloadData(selection: object) {
  const response = await fetch('/api/data-transfer/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(selection),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw Error(typeof error.detail === 'string' ? error.detail : '导出失败');
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download =
    response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || 'tavern-data.zip';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
