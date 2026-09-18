// Build-time policy and alias graph. Validate metadata before stripping it.
import { z } from 'zod';
import { assertGamut, contrastRatio } from './color-validation.ts';

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };
export const isObject = (value: JsonValue | undefined): value is JsonObject =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const name = z.string().regex(/^[a-z][a-zA-Z0-9]*(?:\.[a-z][a-zA-Z0-9]*)*$/);
const pattern = z.string().regex(/^[a-z][a-zA-Z0-9]*(?:\.[a-z][a-zA-Z0-9]*)*(?:\.\*)?$/);
const reference = z.string().regex(/^\{[^{}]+\}$/);
const policySchema = z.strictObject({
  semantic: name,
  kind: z.enum(['palette', 'roles']),
  allowedGamut: z.enum(['srgb', 'display-p3']),
  usage: z.strictObject({ allow: z.array(pattern).min(1), deny: z.array(pattern).default([]) }),
  accessibility: z.strictObject({
    colorIndependentMeaning: z.boolean(),
    contrastTextMin: z.number().min(4.5).max(21).optional(),
    nonText: z.strictObject({ against: z.array(reference).min(1), minimum: z.number().min(3).max(21) }).optional(),
  }),
  derived: z.strictObject({
    scope: z.enum(['none', 'ephemeral-only']), colorSpace: z.literal('oklch'),
    operations: z.array(z.enum(['alpha', 'mix-white', 'mix-black', 'mix-transparent'])),
  }),
  provenance: z.strictObject({ owner: z.string().trim().min(1), decision: z.string().trim().min(1), introduced: z.string().regex(/^\d{4}-\d{2}$/) }),
  stability: z.enum(['stable', 'reserved', 'experimental', 'deprecated']),
});
export type IntentPolicy = z.infer<typeof policySchema>;
const quartet = ['main', 'light', 'dark', 'contrastText'] as const;
const ref = (value: JsonValue): string | undefined =>
  typeof value === 'string' ? /^\{([^{}]+)\}$/.exec(value)?.[1] : undefined;
const matches = (value: string, rule: string): boolean =>
  rule.endsWith('.*') ? value.startsWith(rule.slice(0, -1)) : value === rule;
const isFinancialPath = (path: string): boolean =>
  /^tier2\.intents\.(light|dark)\.financial(?:\.|$)/.test(path)
  || /^tier1\.colorFamily\.financial(?:\.|$)/.test(path);
export const bridgeIntentRoles = {
  measurement: 'palette', financial: 'palette',
  'action.primary': 'palette', 'action.secondary': 'palette', 'action.state': 'roles',
  'feedback.success': 'palette', 'feedback.warning': 'palette', 'feedback.error': 'palette', 'feedback.info': 'palette',
  sentiment: 'roles', communication: 'roles', surface: 'roles', text: 'roles', chart: 'roles',
  'legacy.accent': 'palette', 'legacy.calm': 'palette',
} as const;
// Existing MUI-shaped paths are compatibility adapters, not new meanings.
const adapterRoles: Record<string, string> = {
  primary: 'action.primary', secondary: 'action.secondary', accent: 'legacy.accent', calm: 'legacy.calm',
  action: 'action.state', success: 'feedback.success', warning: 'feedback.warning', error: 'feedback.error', info: 'feedback.info',
  background: 'surface', divider: 'surface', placeholder: 'surface', surface: 'surface',
  text: 'text', communication: 'communication', chart: 'chart',
};
function adapterTarget(path: string): string | undefined {
  const match = /^tier2\.colorSchemes\.(light|dark)\.([^.]+)(?:\.(.+))?$/.exec(path);
  if (!match || !adapterRoles[match[2]]) return undefined;
  const [, scheme, role, field] = match;
  const prefix = 'tier2.intents.' + scheme + '.';
  if (role === 'background') return prefix + 'surface.' + (field === 'default' ? 'canvas' : field);
  if (role === 'divider' || role === 'placeholder') return prefix + 'surface.' + role;
  return prefix + adapterRoles[role] + (field ? '.' + field : '');
}

export type TokenGraph = {
  resolved: JsonObject;
  policies: Map<string, IntentPolicy>;
  resolve: (path: string) => JsonValue;
};
export function buildTokenGraph(raw: JsonObject): TokenGraph {
  const nodes = new Map<string, JsonValue>();
  const policies = new Map<string, IntentPolicy>();
  function resolvePolicy(value: JsonValue, stack: string[] = []): JsonValue {
    const target = ref(value);
    if (!target) return value;
    if (stack.includes(target)) throw new Error('Circular intent policy reference: ' + target);
    let result: JsonValue = raw;
    for (const key of target.split('.')) {
      if (!isObject(result) || !Object.hasOwn(result, key)) throw new Error('Unknown intent policy reference: ' + target);
      result = result[key];
    }
    return resolvePolicy(result, [...stack, target]);
  }
  function collect(value: JsonValue, path: string): void {
    nodes.set(path, value);
    if (isObject(value)) {
      if ('_intent' in value) {
        const parsed = policySchema.safeParse(resolvePolicy(value._intent));
        if (!parsed.success) throw new Error(path + ' has invalid _intent: ' + parsed.error.message);
        const policy = parsed.data;
        if (!policy.semantic.startsWith('decorative.') && !policy.accessibility.colorIndependentMeaning) {
          throw new Error(path + ': essential intents require color-independent meaning');
        }
        if (policy.allowedGamut === 'display-p3' && !policy.semantic.startsWith('decorative.')) {
          throw new Error(path + ': essential intents require srgb');
        }
        if ((policy.derived.scope === 'none') !== (policy.derived.operations.length === 0)) {
          throw new Error(path + ': derivation scope and operations disagree');
        }
        if (policy.kind === 'palette') {
          for (const key of quartet) {
            if (typeof value[key] !== 'string') throw new Error(path + ' requires authored ' + key);
          }
          if (policy.accessibility.contrastTextMin === undefined) throw new Error(path + ' requires contrastTextMin');
          if (Object.keys(value).some((key) => key !== '_intent' && !quartet.includes(key as typeof quartet[number]))) {
            throw new Error(path + ': unknown palette field');
          }
        } else if (policy.accessibility.contrastTextMin !== undefined || policy.accessibility.nonText !== undefined) {
          throw new Error(path + ': pair obligations belong to palette intents');
        }
        policies.set(path, policy);
      }
      for (const [key, child] of Object.entries(value)) {
        if (key.startsWith('_')) continue;
        if (key.includes('.')) throw new Error(path + ': token keys cannot contain dots');
        collect(child, path ? path + '.' + key : key);
      }
    } else if (Array.isArray(value)) value.forEach((child, index) => collect(child, path + '.' + index));
  }
  collect(raw, '');
  type Resolution = { value: JsonValue; dependencies: Set<string> };
  const cache = new Map<string, Resolution>();
  function resolve(path: string, stack: string[] = []): Resolution {
    if (stack.includes(path)) throw new Error('Circular token reference: ' + [...stack, path].join(' -> '));
    const cached = cache.get(path);
    if (cached) return cached;
    const next = [...stack, path];
    if (!nodes.has(path)) {
      // Resolve paths through object-valued aliases without losing provenance.
      const parts = path.split('.');
      for (let i = parts.length - 1; i > 0; i--) {
        const prefix = parts.slice(0, i).join('.');
        const target = ref(nodes.get(prefix) ?? null);
        if (target) {
          if (next.includes(prefix)) throw new Error('Circular token reference: ' + [...next, prefix].join(' -> '));
          const result = resolve(target + '.' + parts.slice(i).join('.'), [...next, prefix]);
          return { value: result.value, dependencies: new Set([prefix, target, ...result.dependencies]) };
        }
      }
      throw new Error('Unknown token reference: {' + path + '}');
    }
    const value = nodes.get(path)!;
    const target = ref(value);
    let result: Resolution;
    if (target) {
      const source = resolve(target, next);
      result = { value: source.value, dependencies: new Set([target, ...source.dependencies]) };
    } else if (isObject(value) || Array.isArray(value)) {
      const dependencies = new Set<string>();
      const entries = Object.keys(value).filter((key) => !key.startsWith('_')).sort().map((key) => {
        const child = resolve(path ? path + '.' + key : key, next);
        child.dependencies.forEach((dependency) => dependencies.add(dependency));
        return [key, child.value] as const;
      });
      result = { value: Array.isArray(value) ? Object.keys(value).map((key) => resolve(path + '.' + key, next).value) : Object.fromEntries(entries), dependencies };
    } else result = { value, dependencies: new Set() };
    cache.set(path, result);
    return result;
  }
  function owner(path: string): IntentPolicy | undefined {
    for (let p = path; p; p = p.slice(0, p.lastIndexOf('.'))) {
      const policy = policies.get(p);
      if (policy) return policy;
      if (!p.includes('.')) break;
    }
    return undefined;
  }
  function consumerSemantic(path: string): string {
    const policy = owner(path);
    if (policy) return policy.semantic;
    const role = /^tier2\.colorSchemes\.(?:light|dark)\.([^.]+)/.exec(path)?.[1];
    return (role && adapterRoles[role]) || 'component';
  }
  for (const [path, value] of nodes) {
    const result = resolve(path);
    const expected = adapterTarget(path);
    if (expected && typeof value === 'string' && ref(value) !== expected) {
      throw new Error(path + ': adapter must reference {' + expected + '}');
    }
    if (ref(value)) {
      for (const dependency of result.dependencies) {
        if (isFinancialPath(dependency) && !isFinancialPath(path)) throw new Error(path + ': financial tokens are reserved');
        const target = owner(dependency);
        if (target) {
          const consumer = consumerSemantic(path);
          if (target.usage.deny.some((rule) => matches(consumer, rule)) || !target.usage.allow.some((rule) => matches(consumer, rule))) {
            throw new Error(path + ': ' + target.semantic + ' does not allow consumer ' + consumer);
          }
        }
      }
      if (isFinancialPath(path) && [...result.dependencies].some((p) => /\.chart\.|\.dataViz\./.test(p))) {
        throw new Error(path + ': financial must not alias chart or opportunity colors');
      }
    }
    const policy = owner(path);
    if (policy && !isObject(value) && typeof value !== 'string') throw new Error(path + ': intent values must be named colors or effects');
    if (typeof value !== 'string') continue;
    if (path.startsWith('tier2.intents.') && !policy) throw new Error(path + ': ungoverned intent value');
    const colorValue = result.value;
    if (typeof colorValue !== 'string') {
      if (policy) throw new Error(path + ': intent leaf must resolve to a color or effect');
      continue;
    }
    if (/(#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(|(?<!ok)l(?:ab|ch)\(|oklab\(|color\()/i.test(colorValue)) {
      throw new Error(path + ': design color tokens must use OKLCH');
    }
    if (/color-mix\(|var\(|oklch\(\s*from\b/i.test(colorValue)) throw new Error(path + ': durable tokens cannot use runtime derivation');
    const effect = /\.(elevation|overlay|rim|glow)$/.test(path);
    if ((policy && !effect) || /^tier1\.(brandPalette|colorFamily|onColor)\./.test(path)) {
      assertGamut(colorValue, policy?.allowedGamut ?? 'srgb', path);
    }
    const colors = colorValue.match(/oklch\([^)]*\)/gi) ?? [];
    if (policy && colors.length === 0) throw new Error(path + ': intent values require a color or color-bearing effect');
    for (const color of colors) assertGamut(color, policy?.allowedGamut ?? 'srgb', path);
  }
  for (const [path, policy] of policies) {
    if (policy.kind !== 'palette') continue;
    const tones = resolve(path).value as JsonObject;
    const ratio = contrastRatio(tones.contrastText as string, tones.main as string);
    if (ratio < policy.accessibility.contrastTextMin!) throw new Error(path + '.contrastText has ' + ratio.toFixed(2) + ':1 contrast');
    for (const against of policy.accessibility.nonText?.against ?? []) {
      const background = resolve(ref(against)!).value;
      if (typeof background !== 'string') throw new Error(path + ': non-text backdrop must resolve to a color');
      if (contrastRatio(tones.main as string, background) < policy.accessibility.nonText!.minimum) throw new Error(path + ': non-text contrast fails');
    }
  }
  return { resolved: resolve('').value as JsonObject, policies, resolve: (path) => resolve(path).value };
}
/** Required roles are not inferred from whatever metadata survives. */
export function validateBridgeIntents(graph: TokenGraph): void {
  for (const scheme of ['light', 'dark']) {
    for (const [role, kind] of Object.entries(bridgeIntentRoles)) {
      const path = 'tier2.intents.' + scheme + '.' + role;
      const policy = graph.policies.get(path);
      const semantic = role === 'financial' ? 'financial.value' : role;
      if (!policy || policy.semantic !== semantic || policy.kind !== kind) throw new Error(path + ': missing or mismatched intent contract');
      if (role === 'financial' && policy.stability !== 'reserved') throw new Error(path + ': financial must remain reserved');
      if (role.startsWith('legacy.') && policy.stability !== 'deprecated') throw new Error(path + ': compatibility intents must be deprecated');
      if (role !== 'financial' && !role.startsWith('legacy.') && policy.stability !== 'stable') throw new Error(path + ': public intents must remain stable');
    }
  }
}

/** Reserved families are checked but never emitted into application tokens. */
export function publicIntents(graph: TokenGraph): JsonObject {
  const intents = structuredClone(graph.resolve('tier2.intents')) as JsonObject;
  for (const [path, policy] of graph.policies) {
    if (!path.startsWith('tier2.intents.') || policy.stability !== 'reserved') continue;
    const keys = path.slice('tier2.intents.'.length).split('.');
    let parent = intents;
    for (const key of keys.slice(0, -1)) parent = parent[key] as JsonObject;
    delete parent[keys[keys.length - 1]];
  }
  return intents;
}
