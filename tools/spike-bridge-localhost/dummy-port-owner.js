import http from 'node:http';

const port = 17871;
const server = http.createServer((_request, response) => {
  response.writeHead(200, { 'Content-Type': 'application/json' });
  response.end(`${JSON.stringify({ owner: 'unrelated-dummy-process', pid: process.pid })}\n`);
});

server.on('error', (error) => {
  process.stderr.write(`DUMMY_ERROR ${error.code || error.message}\n`);
  process.exit(1);
});

server.listen({ host: '127.0.0.1', port, exclusive: true }, () => {
  process.stdout.write(`DUMMY_READY ${JSON.stringify({ pid: process.pid, host: '127.0.0.1', port })}\n`);
});

const shutdown = () => server.close(() => process.exit(0));
process.once('SIGINT', shutdown);
process.once('SIGTERM', shutdown);
