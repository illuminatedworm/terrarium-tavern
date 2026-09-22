import path from 'node:path';
import { build } from 'esbuild';
const root=process.cwd();
const aliases={
  '@/function/schema':'vendor/mvu/schema.ts', '@/variable_def':'vendor/mvu/variable_def.ts',
  '@/i18n':'compat/upstream-host.ts', '@/store':'compat/upstream-host.ts', '@/util':'compat/upstream-host.ts',
  '@util/common':'compat/upstream-host.ts',
};
await build({
  stdin:{contents:`export {updateVariables,extractCommands,parseParameters,applyTemplate} from './vendor/mvu/update_variables.ts';export {generateSchema,cleanUpMetadata,reconcileAndApplySchema} from './vendor/mvu/schema.ts';export {variable_events} from './vendor/mvu/variable_def.ts';`,resolveDir:root,loader:'ts'},
  bundle:true,format:'iife',globalName:'MVUReference',platform:'browser',target:'es2022',
  outfile:'src/tavern/static/mvu-core.js',
  plugins:[{name:'mvu-host-adapter',setup(b){b.onResolve({filter:/^@/},a=>aliases[a.path]?{path:path.join(root,aliases[a.path])}:undefined);}}],
});
