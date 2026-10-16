// Model controls documented by Google; mirrored in core/gemini_capabilities.py.
export function geminiControls(api: string, model: string) {
  const match = model.toLowerCase().match(/(?:^|\/)gemini-(\d+)(?:\.(\d+))?(?=-|$)/);
  if (!['gemini', 'chat_completions'].includes(api) || !match) return { sampling: true, explicitCache: true };
  const major = Number(match[1]),
    minor = Number(match[2] || 0);
  return {
    sampling: !(
      major >= 4 ||
      (major === 3 &&
        (minor >= 6 || (minor === 5 && /gemini-3\.5-flash-lite(?=-|$)/.test(model.toLowerCase()))))
    ),
    explicitCache: major < 4,
  };
}
