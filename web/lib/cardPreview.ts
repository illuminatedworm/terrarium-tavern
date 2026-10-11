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
    const initializationOnly =
      !enabled && audience !== 'tracker_mvu' && /\[initvar\]/i.test(effective.comment || '');
    return {
      key,
      title: effective.comment || effective.name || `条目 ${index + 1}`,
      content: String(effective.content || ''),
      enabled,
      stateLabel: enabled ? '' : initializationOnly ? '仅初始化' : '停用',
      trigger: effective.constant ? '常驻' : '按需',
      audience,
      purpose:
        ({ writer: '叙事', tracker_mvu: '变量规则', runtime_init: '初始化' } as Record<string, string>)[
          audience
        ] || audience,
      keywords: [effective.keys || effective.key || []].flat().map(String).filter(Boolean),
    };
  });
}
