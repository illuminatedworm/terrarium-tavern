// Only reads the downloaded manifest. There is no network access during bundling.
import fs from 'node:fs';
import path from 'node:path';
import { build } from 'esbuild';
const [manifestPath, outputPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const plugin = {
  name: 'pinned-card-dependencies', setup(builder) {
    builder.onResolve({filter: /^entry$/}, () => ({path:'entry',namespace:'card'}));
    builder.onLoad({filter: /^entry$/, namespace:'card'}, () => ({contents: manifest.source,loader:'js'}));
    builder.onResolve({filter: /.*/, namespace:'card'}, args => {
      if (['lodash','jquery','zod','yaml'].includes(args.path)) return {path:args.path,namespace:'global'};
      const url = /^https:\/\//.test(args.path) ? args.path : args.importer === 'entry' ? args.path : new URL(args.path,args.importer).href;
      if (!manifest.modules[url]) throw Error(`Unresolved dependency: ${url}`);
      return {path:url,namespace:'card'};
    });
    builder.onLoad({filter: /.*/,namespace:'global'},args=>({contents:`module.exports=globalThis[${JSON.stringify({lodash:'_',jquery:'$',zod:'z',yaml:'YAML'}[args.path])}];`,loader:'js'}));
    builder.onLoad({filter: /.*/,namespace:'card'},args=>({contents:fs.readFileSync(manifest.modules[args.path],'utf8'),loader:'js'}));
  },
};
await build({entryPoints:['entry'],bundle:true,format:'iife',platform:'browser',target:'es2022',outfile:outputPath,plugins:[plugin]});
