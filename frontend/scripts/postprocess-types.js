import fs from "fs";  
import path from "path";  
import ts from "typescript";  
import { fileURLToPath } from "url";  
  
// Needed because __dirname is not available in ESM  
const __filename = fileURLToPath(import.meta.url);  
const __dirname = path.dirname(__filename);  
  
// ===== REST TYPES =====  
const restTypesPath = path.resolve(__dirname, "../src/types/backend.ts");  
let restContent = fs.readFileSync(restTypesPath, "utf8");  
  
const restSourceFile = ts.createSourceFile(  
  "backend.ts",  
  restContent,  
  ts.ScriptTarget.ESNext,  
  true  
);  
  
let schemaNames = [];  
  
function visitRest(node) {  
  if (  
    ts.isPropertySignature(node) &&  
    node.name.getText() === "schemas" &&  
    node.type &&  
    ts.isTypeLiteralNode(node.type)  
  ) {  
    node.type.members.forEach((member) => {  
      if (ts.isPropertySignature(member)) {  
        const name = member.name.getText();  
        if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {  
          schemaNames.push(name);  
        }  
      }  
    });  
  }  
  ts.forEachChild(node, visitRest);  
}  
  
visitRest(restSourceFile);  
  
// Only keep hard-coded names if they actually exist  
["ChatThread", "Message"].forEach((name) => {  
  if (schemaNames.includes(name)) {  
    console.log(`[postprocess-types] Keeping schema: ${name}`);  
  } else {  
    console.warn(`[postprocess-types] Skipping missing schema: ${name}`);  
  }  
});  
  
console.log(`[postprocess-types] REST schemas: ${schemaNames.join(", ")}`);  
  
const restExportLines = schemaNames.map(  
  (name) => `export type ${name} = components["schemas"]["${name}"];`  
);  
  
restContent += `\n\n// Named exports for schemas\n${restExportLines.join("\n")}\n`;  
fs.writeFileSync(restTypesPath, restContent, "utf8");  
console.log("[postprocess-types] backend.ts updated with named exports");  
  
// ===== WS TYPES =====  
const wssTypesPath = path.resolve(__dirname, "../src/types/backend-wss.ts");  
  
if (fs.existsSync(wssTypesPath)) {  
  let wssContent = fs.readFileSync(wssTypesPath, "utf8");  
  const wssSourceFile = ts.createSourceFile(  
    "backend-wss.ts",  
    wssContent,  
    ts.ScriptTarget.ESNext,  
    true  
  );  
  
  let wssTypeNames = [];  
  
  function visitWss(node) {  
    if (  
      (ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node)) &&  
      /^[A-Za-z_][A-Za-z0-9_]*$/.test(node.name.text)  
    ) {  
      wssTypeNames.push(node.name.text);  
    }  
    ts.forEachChild(node, visitWss);  
  }  
  
  visitWss(wssSourceFile);  
  
  console.log(`[postprocess-types] WS types: ${wssTypeNames.join(", ")}`);  
  
  // Append explicit named exports for WS types if needed  
  const wssExportLines = wssTypeNames  
    .filter(  
      (name) =>  
        !new RegExp(`export type ${name}\\b`).test(wssContent) &&  
        !new RegExp(`export interface ${name}\\b`).test(wssContent)  
    )  
    .map((name) => `export type { ${name} };`);  
  
  if (wssExportLines.length > 0) {  
    wssContent += `\n\n// Explicit named exports for WS message types\n${wssExportLines.join("\n")}\n`;  
  }  
  
  // 🔹 Append OutgoingWssMessage union dynamically from all *Msg interfaces  
  if (!/export type OutgoingWssMessage\b/.test(wssContent)) {  
    const msgInterfaces = wssTypeNames.filter((name) => name.endsWith("Msg"));  
    if (msgInterfaces.length) {  
      wssContent += `\n\n// Discriminated union for all WS outgoing messages\nexport type OutgoingWssMessage =\n  | ${msgInterfaces.join("\n  | ")};\n`;  
      console.log("[postprocess-types] Added OutgoingWssMessage union type");  
    } else {  
      console.warn("[postprocess-types] No *Msg interfaces found for union");  
    }  
  } else {  
    console.log("[postprocess-types] OutgoingWssMessage already exists");  
  }  
  
  fs.writeFileSync(wssTypesPath, wssContent, "utf8");  
  console.log("[postprocess-types] backend-wss.ts updated");  
} else {  
  console.warn("[postprocess-types] No backend-wss.ts found — skipping WS postprocess");  
}  