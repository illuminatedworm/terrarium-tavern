/** Images cross the authenticated bridge as data URLs; the frame keeps network access denied. */
export function installImageLoader(load: (url: string) => Promise<string>, report: (error: unknown) => void) {
  const set = Element.prototype.setAttribute;
  const remove = Element.prototype.removeAttribute;
  const requests = new WeakMap<Element, Map<string, { source: string }>>();
  const pending = new Map<string, Promise<string>>();
  const remote = (url: string) => /^https?:\/\//i.test(url);
  const cssRemote = (value: string) => /url\(\s*["']?https?:\/\//i.test(value);
  const fetch = (url: string) => {
    let task = pending.get(url);
    if (!task) {
      task = load(url).finally(() => pending.delete(url));
      pending.set(url, task);
    }
    return task;
  };
  const isImage = (el: Element, name: string) =>
    (el instanceof HTMLImageElement && ['src', 'srcset'].includes(name)) ||
    (el instanceof HTMLSourceElement && name === 'srcset') ||
    (el.tagName.toLowerCase() === 'image' && ['href', 'xlink:href'].includes(name)) ||
    (el instanceof HTMLVideoElement && name === 'poster');
  const rewrite = async (value: string, name: string) => {
    if (name === 'style') {
      const urls = [...value.matchAll(/url\(\s*(["']?)(https?:\/\/.*?)\1\s*\)/gi)];
      for (const match of urls) value = value.replace(match[0], `url("${await fetch(match[2])}")`);
      return value;
    }
    if (name !== 'srcset') return remote(value) ? fetch(value) : value;
    if (value.trimStart().startsWith('data:')) return value;
    return (
      await Promise.all(
        value.split(',').map(async (item) => {
          const [url, ...descriptor] = item.trim().split(/\s+/);
          return [remote(url) ? await fetch(url) : url, ...descriptor].join(' ');
        }),
      )
    ).join(', ');
  };
  function update(el: Element, name: string, value: string) {
    if (
      (!isImage(el, name) && name !== 'style') ||
      !(name === 'style' ? cssRemote(value) : /https?:\/\//i.test(value))
    ) {
      requests.get(el)?.delete(name);
      set.call(el, name, value);
      return;
    }
    let attrs = requests.get(el);
    if (!attrs) requests.set(el, (attrs = new Map()));
    const state = { source: value };
    attrs.set(name, state);
    // Remove the old candidate while resolving: stale completions cannot repaint it.
    remove.call(el, name);
    void rewrite(value, name)
      .then((result) => {
        if (attrs.get(name) === state) set.call(el, name, result);
      })
      .catch((error) => {
        if (attrs.get(name) !== state) return;
        el.dispatchEvent(new Event('error'));
        report(error);
      });
  }
  Element.prototype.setAttribute = function (name, value) {
    const attribute = name.toLowerCase();
    if (isImage(this, attribute) || attribute === 'style') update(this, attribute, String(value));
    else set.call(this, name, value);
  };
  Element.prototype.removeAttribute = function (name) {
    requests.get(this)?.delete(name.toLowerCase());
    remove.call(this, name);
  };
  for (const [prototype, name] of [
    [HTMLImageElement.prototype, 'src'],
    [HTMLImageElement.prototype, 'srcset'],
    [HTMLSourceElement.prototype, 'srcset'],
    [HTMLVideoElement.prototype, 'poster'],
  ] as const) {
    const descriptor = Object.getOwnPropertyDescriptor(prototype, name)!;
    Object.defineProperty(prototype, name, {
      ...descriptor,
      get() {
        return requests.get(this)?.get(name)?.source ?? descriptor.get!.call(this);
      },
      set(value) {
        update(this, name, String(value));
      },
    });
  }
  // Parser-created HTML, innerHTML, namespaced SVG hrefs and style attributes do
  // not use the JS setters. Their remote requests remain blocked by CSP until rewritten.
  const scan = (el: Element) => {
    for (const attr of Array.from(el.attributes)) {
      if (
        (isImage(el, attr.name) && /https?:\/\//i.test(attr.value)) ||
        (attr.name === 'style' && cssRemote(attr.value))
      )
        update(el, attr.name, attr.value);
    }
  };
  new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'attributes') scan(record.target as Element);
      for (const node of Array.from(record.addedNodes)) {
        if (!(node instanceof Element)) continue;
        scan(node);
        node.querySelectorAll('*').forEach(scan);
      }
    }
  }).observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['src', 'srcset', 'href', 'xlink:href', 'poster', 'style'],
  });
  document.querySelectorAll('*').forEach(scan);
}
