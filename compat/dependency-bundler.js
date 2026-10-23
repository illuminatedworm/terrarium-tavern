// Only the predownloaded module set is readable. No resolver performs a network fetch.
globalThis.__tavernBundle = async ({ source, modules }) => {
  const available = new Set(modules);
  const globals = { lodash: '_', jquery: '$', zod: 'z', yaml: 'YAML' };
  const plugin = {
    name: 'pinned-card-dependencies',
    setup(builder) {
      builder.onResolve({ filter: /^entry$/ }, () => ({ path: 'entry', namespace: 'card' }));
      builder.onLoad({ filter: /^entry$/, namespace: 'card' }, () => ({ contents: source, loader: 'js' }));
      builder.onResolve({ filter: /.*/, namespace: 'card' }, args => {
        if (Object.hasOwn(globals, args.path)) return { path: args.path, namespace: 'global' };
        const url = /^https:\/\//.test(args.path) || args.importer === 'entry'
          ? args.path : new URL(args.path, args.importer).href;
        if (!available.has(url)) throw Error(`Unresolved dependency: ${url}`);
        return { path: url, namespace: 'card' };
      });
      builder.onLoad({ filter: /.*/, namespace: 'global' }, args => ({
        contents: `module.exports=globalThis[${JSON.stringify(globals[args.path])}];`, loader: 'js',
      }));
      builder.onLoad({ filter: /.*/, namespace: 'card' }, async args => ({
        contents: await __tavernHost('module', args.path), loader: 'js',
      }));
    },
  };
  const result = await esbuild.build({
    entryPoints: ['entry'], bundle: true, format: 'iife', platform: 'browser',
    target: 'es2022', write: false, logLevel: 'silent', plugins: [plugin],
  });
  return result.outputFiles[0].text;
};
