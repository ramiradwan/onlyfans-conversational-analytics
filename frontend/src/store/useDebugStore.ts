// src/store/useDebugStore.ts  
import { create } from "zustand";  
  
export type LogSeverity = "info" | "warn" | "error" | "event";  
  
export interface DebugLog {  
  timestamp: string; // e.g., "14:32:10"  
  severity: LogSeverity;  
  message: string;  
}  
  
interface DebugState {  
  logs: DebugLog[];  
  
  // ✅ PURE getters  
  getLogs: () => DebugLog[];  
  
  // actions  
  addLog: (severity: LogSeverity, message: string) => void;  
  clearLogs: () => void;  
}  
  
export const useDebugStore = create<DebugState>((set, get) => ({  
  logs: [],  
  
  // getters  
  getLogs: () => get().logs,  
  
  // actions  
  addLog: (severity, message) =>  
    set((state) => {  
      const timestamp = new Date().toLocaleTimeString([], {  
        hour: "2-digit",  
        minute: "2-digit",  
        second: "2-digit",  
      });  
      const newLog: DebugLog = { timestamp, severity, message };  
      const updated = [...state.logs, newLog];  
      // Keep only last 200 logs for performance  
      return { logs: updated.slice(-200) };  
    }),  
  
  clearLogs: () => set({ logs: [] }),  
}));  