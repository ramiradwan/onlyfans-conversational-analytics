// aliases.config.js  
import path from 'path';  
import { fileURLToPath } from 'url';  
  
const __filename = fileURLToPath(import.meta.url);  
const __dirname = path.dirname(__filename);  
  
// Absolute path to src directory  
const srcDir = path.resolve(__dirname, '../src');  
  
// Aliases for both build tools and tsconfig.json  
export const aliasEntries = {  
  '@': srcDir,  
  '@types': path.join(srcDir, 'types'),  
  '@components': path.join(srcDir, 'components'),  
  '@hooks': path.join(srcDir, 'hooks'),  
  '@store': path.join(srcDir, 'store'),  
  '@theme': path.join(srcDir, 'theme'),  
  '@assets': path.join(srcDir, 'assets'),  
  '@utils': path.join(srcDir, 'utils'),  
  '@views': path.join(srcDir, 'views'),  
  '@layouts': path.join(srcDir, 'layouts'),  
  '@routing': path.join(srcDir, 'routing'),  
  '@services': path.join(srcDir, 'services'),  
  '@common': path.join(srcDir, 'common'),  
  '@config': path.join(srcDir, 'config'),  
};  
  
// Generate tsconfig.json "paths" mapping  
export const tsPaths = Object.fromEntries(  
  Object.entries(aliasEntries).map(([alias, absPath]) => {  
    let rel = path.relative(srcDir, absPath).replace(/\\/g, '/');  
    if (rel === '') {  
      // root alias '@' → map to '*', not '/*'  
      rel = '*';  
    } else {  
      rel += '/*';  
    }  
    return [`${alias}/*`, [rel]];  
  })  
);  