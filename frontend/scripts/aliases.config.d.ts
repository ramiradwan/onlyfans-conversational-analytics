// scripts/aliases.config.d.ts  
  
// Import the runtime JS module just for type information  
import type * as aliases from './aliases.config.js';  
  
/**  
 * All alias keys — automatically inferred from `aliasEntries` in aliases.config.js  
 */  
export type AliasKey = keyof typeof aliases.aliasEntries;  
  
/**  
 * Absolute path mappings for Vite and Node runtime.  
 * Keys are alias names (e.g. "@components"), values are absolute paths.  
 */  
export declare const aliasEntries: typeof aliases.aliasEntries;  
  
/**  
 * Keys for TypeScript `paths` mapping — automatically derived from AliasKey.  
 */  
export type TsPathKey = `${AliasKey}/*`;  
  
/**  
 * TypeScript `paths` mapping — automatically inferred from runtime.  
 */  
export declare const tsPaths: Record<TsPathKey, string[]>;  