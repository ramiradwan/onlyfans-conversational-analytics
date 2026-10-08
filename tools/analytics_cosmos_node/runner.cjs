'use strict';

const { Adapter, SafeError, MAX_BYTES } = require('./adapter.cjs');
const names = ['ANALYTICS_GREMLIN_ENDPOINT', 'ANALYTICS_GREMLIN_KEY',
  'ANALYTICS_GREMLIN_DATABASE', 'ANALYTICS_GREMLIN_GRAPH'];

function emit(value) {
  const line = JSON.stringify(value) + '\n';
  if (Buffer.byteLength(line) > MAX_BYTES) throw new SafeError('invalid_request');
  process.stdout.write(line);
}

async function main() {
  if (!names.every(name => process.env[name])) {
    emit({ id: null, status: 'not_configured', code: 'not_configured' });
    return;
  }
  const gremlin = require('gremlin');
  const endpoint = new URL(process.env.ANALYTICS_GREMLIN_ENDPOINT);
  if (endpoint.protocol !== 'wss:' || endpoint.username || endpoint.password) throw new SafeError('invalid_request');
  const adapter = new Adapter(() => new gremlin.driver.Client(endpoint.href, {
    traversalSource: 'g', mimeType: 'application/vnd.gremlin-v2.0+json', connectOnStartup: false,
    rejectUnauthorized: true,
    authenticator: new gremlin.driver.auth.PlainTextSaslAuthenticator(
      `/dbs/${process.env.ANALYTICS_GREMLIN_DATABASE}/colls/${process.env.ANALYTICS_GREMLIN_GRAPH}`,
      process.env.ANALYTICS_GREMLIN_KEY),
  }));
  let buffer = Buffer.alloc(0);
  try {
    for await (const chunk of process.stdin) {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > MAX_BYTES) throw new SafeError('invalid_request');
      let end;
      while ((end = buffer.indexOf(10)) >= 0) {
        const line = buffer.subarray(0, end);
        buffer = buffer.subarray(end + 1);
        let request;
        try {
          request = JSON.parse(line.toString('utf8'));
          emit(await adapter.execute(request));
        } catch (error) {
          emit({ id: Number.isSafeInteger(request?.id) ? request.id : null, status: 'error',
            code: error instanceof SafeError ? error.code : 'invalid_request',
            server_status: error instanceof SafeError ? error.serverStatus : null });
        }
      }
    }
    if (buffer.length) throw new SafeError('invalid_request');
  } finally {
    await adapter.close();
  }
}

main().catch(() => {
  emit({ id: null, status: 'error', code: 'transport' });
  process.exitCode = 1;
});
