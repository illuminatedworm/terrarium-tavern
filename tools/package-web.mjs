import fs from 'node:fs';
import path from 'node:path';
// Build output is disposable; remove stale hashed bundles before copying.
const root = fs.realpathSync('.');
const output = path.resolve(root, 'src/tavern/static/web');
if (!fs.realpathSync(path.dirname(output)).startsWith(root + path.sep)
    || (fs.existsSync(output) && !fs.realpathSync(output).startsWith(root + path.sep))) {
  throw new Error('Generated web directory must remain inside the project');
}
fs.rmSync(output, {recursive: true, force: true});
fs.cpSync('dist','src/tavern/static/web',{recursive:true});
fs.mkdirSync('src/tavern/static/licenses',{recursive:true});
fs.copyFileSync('vendor/mvu/LICENSE','src/tavern/static/licenses/MagVarUpdate.txt');
fs.copyFileSync('THIRD_PARTY_NOTICES.md','src/tavern/static/licenses/THIRD_PARTY_NOTICES.md');
