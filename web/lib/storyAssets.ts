export type AssetLease = { url: string; blob: Blob; release: () => void };
const assets = new Map<string, { url: string; blob: Blob; refs: number }>();

/** Reference-counted immutable assets. No dormant URLs retain old conversations. */
export function acquireAsset(key: string, blob: () => Blob): AssetLease {
  let entry = assets.get(key);
  if (!entry) {
    // This is a template marker, not a parent-origin Blob URL. Opaque frames
    // create their own URLs from structured-cloned, shared Blob backing stores.
    entry = { url: `tavern-asset:${crypto.randomUUID()}`, blob: blob(), refs: 0 };
    assets.set(key, entry);
  }
  entry.refs++;
  let released = false;
  return {
    url: entry.url,
    blob: entry.blob,
    release() {
      if (released) return;
      released = true;
      if (--entry!.refs === 0) {
        assets.delete(key);
      }
    },
  };
}

export function scriptAsset(source: string) {
  return acquireAsset('script:' + source, () => new Blob([source], { type: 'text/javascript' }));
}

const histories = new WeakMap<object, { lease: AssetLease; refs: number }>();
export function acquireHistory(messages: any[]): AssetLease {
  let entry = histories.get(messages);
  if (!entry) {
    // Factories keep records unparsed until a synchronous legacy getter reads them.
    // All frames load the same immutable script URL, without embedding history in srcdoc.
    const factory = (value: any) => `()=>JSON.parse(${JSON.stringify(JSON.stringify(value))})`;
    const records = messages.map((m, i) =>
      factory({ message_id: i, id: m.id, role: m.role, message: m.raw }),
    );
    const states = messages.map((m) => factory(m.snapshot?.state || {}));
    const owners: Record<string, number>[] = [];
    let current: Record<string, number> = Object.create(null);
    messages.forEach((m, i) => {
      current = Object.assign(Object.create(null), current);
      for (const key of Object.keys(m.snapshot?.state || {})) current[key] = i;
      owners.push(current);
    });
    const source = `(()=>{const r=[${records.join(',')}],v=[${states.join(',')}],o=[${owners.map(factory).join(',')}];
      window.__tavernHistory={length:r.length,read:i=>({...r[i](),variables:v[i]()}),variables:i=>{
        const result=Object.create(null),states=new Map();
        for(const [key,index] of Object.entries(o[i]())){if(!states.has(index))states.set(index,v[index]());result[key]=states.get(index)[key];}return result;
      }};})();`;
    entry = { lease: scriptAsset(source), refs: 0 };
    histories.set(messages, entry);
  }
  entry.refs++;
  let released = false;
  return {
    url: entry.lease.url,
    blob: entry.lease.blob,
    release() {
      if (released) return;
      released = true;
      if (--entry!.refs === 0) {
        entry!.lease.release();
        histories.delete(messages);
      }
    },
  };
}

export function acquireMarkup(html: string) {
  const leases: AssetLease[] = [];
  function media(uri: string) {
    if (
      uri.length < 2048 ||
      !/^data:(?:image\/|font\/|application\/(?:font|x-font|vnd\.ms-fontobject))/i.test(uri)
    )
      return uri;
    try {
      const comma = uri.indexOf(',');
      const header = uri.slice(5, comma);
      const raw = /;base64$/i.test(header) ? atob(uri.slice(comma + 1)) : undefined;
      const payload =
        raw === undefined
          ? new TextEncoder().encode(decodeURIComponent(uri.slice(comma + 1)))
          : Uint8Array.from(raw, (c) => c.charCodeAt(0));
      const lease = acquireAsset(uri, () => new Blob([payload], { type: header.split(';')[0] }));
      leases.push(lease);
      return lease.url;
    } catch {
      return uri;
    }
  }
  const css = (text: string) =>
    text.replace(
      /url\(\s*(["']?)(data:[^\s)'"<>]+)\1\s*\)/gi,
      (_match, _quote, uri) => `url("${media(uri)}")`,
    );
  // Do not rewrite strings or examples inside scripts, comments or raw-text elements.
  const markup = html.replace(
    /<!--[\s\S]*?-->|<(script|textarea|title)\b[^>]*>[\s\S]*?<\/\1\s*>|<style\b([^>]*)>([\s\S]*?)<\/style\s*>|<[a-z][^>]*>/gi,
    (tag, rawTag, attrs, content) => {
      if (rawTag || tag.startsWith('<!--')) return tag;
      if (content !== undefined) {
        // IDs, media queries and other author attributes may be read by scripts.
        // Only anonymous style blocks can become shared stylesheets.
        if (String(attrs).trim()) return `<style${attrs}>${css(content)}</style>`;
        const text = css(content);
        const lease = acquireAsset('css:' + text, () => new Blob([text], { type: 'text/css' }));
        leases.push(lease);
        return `<link rel="stylesheet" href="${lease.url}">`;
      }
      return tag
        .replace(
          /\b(src)\s*=\s*(["'])(data:[^"']+)\2/gi,
          (_match: string, attr: string, quote: string, uri: string) =>
            `${attr}=${quote}${media(uri)}${quote}`,
        )
        .replace(
          /\bstyle\s*=\s*(["'])([\s\S]*?)\1/gi,
          (_match: string, quote: string, value: string) =>
            `style=${quote}${css(value).replaceAll(quote, quote === '"' ? '&quot;' : '&#39;')}${quote}`,
        );
    },
  );
  return { html: markup, leases, release: () => leases.forEach((lease) => lease.release()) };
}

// Refuse suspension if saving would exceed the budget; never discard live user input.
let savedBytes = 0;
export function reservePanelState(value: any, limit = 4 * 1024 * 1024) {
  let json: string;
  try {
    json = JSON.stringify(value);
    if (json === undefined) return undefined;
  } catch {
    return undefined;
  }
  const size = new TextEncoder().encode(json).length;
  if (savedBytes + size > limit) return undefined;
  savedBytes += size;
  let released = false;
  return {
    value: JSON.parse(json),
    release() {
      if (!released) {
        released = true;
        savedBytes -= size;
      }
    },
  };
}
