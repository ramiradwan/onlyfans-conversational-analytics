// src/main.tsx  
import React from "react";  
import { createRoot } from "react-dom/client";  
import App from "./App";  
import { getConfig } from "./utils";  
  
const config = getConfig();  
  
createRoot(document.getElementById("root") as HTMLElement).render(  
  <React.StrictMode>  
    <App config={config} />  
  </React.StrictMode>  
);  