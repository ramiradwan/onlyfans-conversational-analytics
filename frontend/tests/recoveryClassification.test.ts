import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import * as recovery from '../shared-setup/recovery.js';
import { renderSetup } from '../onboarding/view';
import { renderBrowserTask } from '../browser-tasks/view';

const read = (path: string) => readFileSync(path, 'utf8');
const source = (path: string) => ts.createSourceFile(path, read(path), ts.ScriptTarget.Latest, true);
type Map = Readonly<Record<string, recovery.RecoveryClass | null>>;
function classified(mapping: Map, codes: string[]) {
  expect(codes.length).toBeGreaterThan(0);
  for (const code of codes) expect(Object.hasOwn(mapping, code), code).toBe(true);
}
function interfaceCodes(name: string, property: string) {
  const declaration = source('src/protocol/types.ts').statements.find(
    (item): item is ts.InterfaceDeclaration => ts.isInterfaceDeclaration(item) && item.name.text === name);
  const field = declaration?.members.find((item): item is ts.PropertySignature =>
    ts.isPropertySignature(item) && item.name.getText() === property);
  expect(field?.type && ts.isUnionTypeNode(field.type), name + '.' + property).toBe(true);
  return (field!.type as ts.UnionTypeNode).types.flatMap(type =>
    ts.isLiteralTypeNode(type) && ts.isStringLiteral(type.literal) ? [type.literal.text] : []);
}
function objectKeys(path: string, name: string) {
  const declarations = source(path).statements.flatMap(item =>
    ts.isVariableStatement(item) ? [...item.declarationList.declarations] : []);
  const expression = declarations.find(item => item.name.getText() === name)?.initializer;
  expect(expression && ts.isObjectLiteralExpression(expression), name).toBe(true);
  return (expression as ts.ObjectLiteralExpression).properties.map(property => {
    expect(ts.isPropertyAssignment(property)).toBe(true);
    const key = (property as ts.PropertyAssignment).name;
    return ts.isStringLiteral(key) || ts.isIdentifier(key) ? key.text : key.getText();
  });
}

describe('recovery classification', () => {
  it('covers the current desktop and browser protocol enums', () => {
    const desktop = JSON.parse(read('../shared/desktop-onboarding/schema-v2.json'));
    const browser = JSON.parse(read('../shared/browser-tasks/schema.json'));
    classified(recovery.desktopRecovery, desktop.$defs.reason.enum);
    classified(recovery.operationRecovery, desktop.$defs.operation.properties.state.enum);
    classified(recovery.authenticationRecovery, desktop.oneOf[1].properties.authentication.oneOf[1].properties.state.enum);
    classified(recovery.decisionRecovery, desktop.oneOf[1].properties.decision_status.oneOf[1].properties.state.enum);
    classified(recovery.browserRecovery, browser.$defs.observation.properties.state.enum);
  });

  it('covers the current capture, history and protocol reason unions', () => {
    classified(recovery.captureReasonRecovery, interfaceCodes('CaptureStateReportRequest', 'reason'));
    classified(recovery.catchupReasonRecovery, interfaceCodes('CatchupFreshness', 'reason'));
    classified(recovery.historyOwnerRecovery, interfaceCodes('BrowserSurfacePayload', 'history_error_code'));
    classified(recovery.protocolRecovery, interfaceCodes('ProtocolErrorPayload', 'code'));
  });

  it('covers all customer-facing desktop, browser, passkey and activation codes', () => {
    classified(recovery.desktopRecovery, objectKeys('onboarding/view.js', 'errors'));
    classified(recovery.passkeyRecovery, objectKeys('onboarding/view.js', 'passkeyErrors'));
    classified(recovery.browserRecovery, objectKeys('browser-tasks/view.js', 'status'));
    classified(recovery.activationRecovery, objectKeys('src/services/capabilityLicenseApi.ts', 'REFUSAL_MESSAGES'));
  });

  it('distinguishes all recovery classes and rejects unrecognised codes without exposing their text', () => {
    const maps = Object.values(recovery).filter(value => typeof value === 'object') as Map[];
    const classes = new Set(maps.flatMap(mapping => Object.values(mapping).filter(value => value !== null)));
    expect([...classes].sort()).toEqual([
      'account_mismatch', 'authentication_required', 'authorization_refused', 'browser_disconnected',
      'cancelled', 'collection_paused', 'insufficient_evidence', 'invariant_failed', 'operation_pending',
      'outcome_unknown', 'owner_unavailable', 'permission_required', 'review_required',
      'transport_interrupted', 'version_incompatible',
    ].sort());
    for (const map of maps) {
      for (const code of ['unrecognised_owner_code', '__proto__', 'constructor', 'toString', {}]) {
        expect(recovery.recoveryClass(map, code)).toBe('invariant_failed');
      }
      expect(recovery.recoveryClass(map, null)).toBeNull();
    }
    expect(recovery.recoveryClass(recovery.historyRequestRecovery, 'conflict')).toBe('outcome_unknown');
    expect(recovery.recoveryClass(recovery.historyOwnerRecovery, 'software_update_required')).toBe('version_incompatible');
    expect(recovery.recoveryClass(recovery.coverageRecovery, 'consent_revoked')).toBe('collection_paused');
    expect(recovery.recoveryClass(recovery.activationRecovery, 'redemption_mismatch')).toBe('account_mismatch');
    expect(recovery.recoveryClass(recovery.operationRecovery, 'refused')).toBe('authorization_refused');
    expect(recovery.recoveryClass(recovery.findingsRecovery, 'analytics_projection_building')).toBe('operation_pending');
    expect(recovery.recoveryClass(recovery.desktopRecovery, 'connection_unavailable')).toBe('owner_unavailable');
    expect(recovery.recoveryClass(recovery.desktopRecovery, 'session_unavailable')).toBe('invariant_failed');
  });

  it('renders a refused browser read differently from an unavailable owner without offering another sign-in', () => {
    document.documentElement.innerHTML = read('browser-tasks/index.html');
    renderBrowserTask(document, { state: 'service_unavailable', recovery: 'authorization_refused' });
    expect(document.getElementById('task-status')?.textContent).toBe('This browser task is not authorized.');
    expect(document.getElementById('authentication-open')?.hidden).toBe(true);
    renderBrowserTask(document, { state: 'service_unavailable', recovery: 'owner_unavailable' });
    expect(document.getElementById('task-status')?.textContent).toContain('connect right now');
    renderBrowserTask(document, { state: 'private-owner-diagnostic' });
    expect(document.getElementById('task-status')?.textContent).toBe("This browser task couldn't continue.");
    expect(document.getElementById('extension-open')?.hidden).toBe(true);
  });

  it('fails closed on an unknown desktop failure even with a previously permitted action', () => {
    document.documentElement.innerHTML = read('onboarding/index.html');
    renderSetup(document, { connected: true, error: 'private-owner-diagnostic', pending: null,
      snapshot: { generation: 'g', journey_id: 'j', next_task: 'browser-access', facts: { installation: 'missing' },
        allowed_actions: ['browser.access'], pending_operations: [], reason: 'none' } });
    expect(document.getElementById('task-title')?.textContent).toBe('Setup couldn’t continue');
    expect(document.getElementById('task-action')?.hidden).toBe(true);
    expect(document.getElementById('setup-code-open')?.hidden).toBe(true);
  });
});
