import { auditPackagedSnow } from '../companion-snow-release.mjs';
const { release } = await auditPackagedSnow();
console.log(JSON.stringify({ result: 'passed', ...release.outputs }, null, 2));
