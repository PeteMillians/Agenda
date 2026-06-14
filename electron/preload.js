// ================================================
// electron/preload.js (Security Bridge)
// This file runs in the context of the web page and securely exposes necessary APIs 
// from Node.js process to the isolated React renderer process.
// ================================================

const { contextBridge, ipcRenderer } = require('electron');

/**
 * Exposes a safe API to communicate with the main Electron process.
 */
contextBridge.exposeInMainWorld('api', {
    // Example: Sending a message when a button is clicked in React
    sendToMainProcess: (channel, data) => ipcRenderer.send(channel, data), 
    // Example: Listening for events from the main process (e.g., "AppReady")
    on: (channel, callback) => ipcRenderer.on(channel, (event, ...args) => callback(...args))
});