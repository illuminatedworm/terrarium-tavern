import { isInitVar, loreTitle } from './loreBook';
import { audiences } from './loreFilter';

/** Read-only projections; all values come from the selected draft, never its source card. */
export function previewCardData(document: Record<string, any>): Record<string, any> {
  return (document.spec ? document.data : document) || {};
}

export function previewLore(document: Record<string, any>) {
  const entries = previewCardData(document).character_book?.entries || [];
  return Object.entries(entries).map(([key, raw], index) => {
    const entry = raw as Record<string, any>;
    const effective = { ...entry, ...entry.extensions };
    const audience = effective.terrarium?.audience || 'writer';
    const enabled = effective.enabled ?? !effective.disable;
    let stateLabel = enabled ? '' : '未启用';
    if (!enabled && isInitVar(effective)) {
      stateLabel = audience === 'tracker_mvu' ? '初始化与变量规则' : '仅初始化';
    }
    return {
      key,
      title: loreTitle(effective) || `条目 ${index + 1}`,
      content: String(effective.content || ''),
      enabled,
      stateLabel,
      trigger: effective.constant ? '常驻' : '条件触发',
      audience,
      purpose: audiences[audience] || audience,
      keywords: [effective.keys || effective.key || []].flat().map(String).filter(Boolean),
    };
  });
}
