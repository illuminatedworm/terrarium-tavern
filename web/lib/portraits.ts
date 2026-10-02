/** Only request portraits backed by an imported file or embedded resource. */
export function cardPortraitUrl(card: any): string {
  if (!card?.id || card._draftOnly) return '';
  let digest = card.container === 'png' ? card.original : '';
  if (!digest) {
    const data = card.document?.spec ? card.document.data : card.document;
    const assets = Array.isArray(data?.assets) ? data.assets : [];
    for (const asset of assets) {
      if (asset?.type !== 'icon' || typeof asset.uri !== 'string') continue;
      const path = asset.uri.replace(/^embeded:\/\//, '');
      if (!/\.(png|jpe?g|webp|gif|avif)$/i.test(path)) continue;
      digest = card.resources?.[path];
      if (digest) break;
    }
  }
  if (!digest) return '';
  const owner = card.session_id
    ? `sessions/${encodeURIComponent(card.session_id)}`
    : `cards/${encodeURIComponent(card.id)}`;
  return `/api/${owner}/portrait?v=${encodeURIComponent(digest)}`;
}

/** Session list rows serialize their snapshot; open sessions return an object. */
export function sessionPortraitUrl(session: any): string {
  if (!session?.card_data) return '';
  const card = typeof session.card_data === 'string' ? JSON.parse(session.card_data) : session.card_data;
  return cardPortraitUrl({ ...card, session_id: session.id });
}
