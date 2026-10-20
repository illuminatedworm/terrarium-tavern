type RegexScript = Record<string, any>;

const isObject = (value: unknown): value is Record<string, any> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

/**
 * Append SillyTavern regex exports (one script object or an array of them).
 *
 * Existing scripts stay untouched and in order; only an imported ID that is
 * missing or already used gets a fresh UUID, matching ST's import behavior.
 */
export function appendRegexScripts(current: unknown, imported: unknown): RegexScript[] {
  if (current !== undefined && !Array.isArray(current)) throw Error('extensions.regex_scripts 必须为数组');
  const incoming = Array.isArray(imported) ? imported : [imported];
  if (!incoming.length) throw Error('文件中没有正则脚本');
  incoming.forEach((script, index) => {
    if (!isObject(script) || typeof script.findRegex !== 'string')
      throw Error(`第 ${index + 1} 个正则脚本缺少 findRegex 文本`);
    if (script.replaceString !== undefined && typeof script.replaceString !== 'string')
      throw Error(`第 ${index + 1} 个正则脚本的 replaceString 必须为文本`);
  });
  const scripts: RegexScript[] = JSON.parse(JSON.stringify(current ?? []));
  const used = new Set(scripts.map((script) => script?.id).filter((id) => id !== undefined && id !== null));
  for (const raw of incoming) {
    const script: RegexScript = JSON.parse(JSON.stringify(raw));
    if (script.id === undefined || script.id === null || script.id === '' || used.has(script.id)) {
      do script.id = crypto.randomUUID();
      while (used.has(script.id));
    }
    used.add(script.id);
    scripts.push(script);
  }
  return scripts;
}
