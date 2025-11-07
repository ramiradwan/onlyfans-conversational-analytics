// scripts/postprocess-types.js  
const fs = require("fs");  
const path = require("path");  
const ts = require("typescript");  
  
const typesPath = path.resolve(__dirname, "../src/types/backend.ts");  
let content = fs.readFileSync(typesPath, "utf8");  
  
const sourceFile = ts.createSourceFile(  
  "backend.ts",  
  content,  
  ts.ScriptTarget.ESNext,  
  /*setParentNodes*/ true  
);  
  
let schemaNames = [];  
  
function visit(node) {  
  // Look for: PropertySignature named 'schemas'  
  if (  
    ts.isPropertySignature(node) &&  
    node.name.getText() === "schemas" &&  
    node.type &&  
    ts.isTypeLiteralNode(node.type)  
  ) {  
    // Each property here is a schema  
    node.type.members.forEach(member => {  
      if (ts.isPropertySignature(member)) {  
        const name = member.name.getText();  
        if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {  
          schemaNames.push(name);  
        }  
      }  
    });  
  }  
  ts.forEachChild(node, visit);  
}  
  
visit(sourceFile);  
  
// Always ensure ChatThread and Message  
["ChatThread", "Message"].forEach(name => {  
  if (!schemaNames.includes(name)) schemaNames.push(name);  
});  
  
console.log(`[postprocess-types] Found schemas: ${schemaNames.join(", ")}`);  
  
const exportLines = schemaNames.map(  
  name => `export type ${name} = components["schemas"]["${name}"];`  
);  
  
content += "\n\n// Named exports for schemas\n" + exportLines.join("\n") + "\n";  
fs.writeFileSync(typesPath, content, "utf8");  
  
console.log("[postprocess-types] backend.ts updated with named exports");  