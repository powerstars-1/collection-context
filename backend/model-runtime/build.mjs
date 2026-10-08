import {build} from '../../frontend/node_modules/esbuild/lib/main.js';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.dirname(fileURLToPath(import.meta.url));
await build({entryPoints:[path.join(root,'worker.mjs')],bundle:true,platform:'node',format:'esm',target:'node22',
  outfile:path.resolve(root,'../src/collection_context/processing/pi_worker.mjs'),legalComments:'eof',
  banner:{js:"import {createRequire as piCreateRequire} from 'node:module'; const require=piCreateRequire(import.meta.url);"}});
