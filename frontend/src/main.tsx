import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './index.css'; // <-- IMPORT THE GLOBAL CSS

// Find the root element in index.html
const rootElement = document.getElementById('root');

if (!rootElement) {
  throw new Error('Failed to find the root element. Check your index.html.');
}

const root = ReactDOM.createRoot(rootElement);

// Render the application inside React's StrictMode
// for development-time checks.
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);