// ================================================
// electron/main.js (Main Process)
// This is the entry point for the native desktop application shell.
// It handles window creation and IPC communication with React.
// ================================================

const { app, BrowserWindow } = require('electron');
const path = require('path');

function createWindow () {
    // Create the browser window. We set the window to load from our local client build output.
    const win = new BrowserWindow({
        width: 1200,
        height: 800,
        webPreferences: {
            preload: path.join(__dirname, 'electron-preload.js'), // Secure bridge for IPC
            nodeIntegration: false, // Best practice: Keep Node isolated in the renderer process
            contextIsolation: true, // Essential security measure
        }
    });

    // Load the local React application's development server (or built index.html)
    win.loadURL('http://localhost:5173'); 

    // Open DevTools for debugging purposes during development
    // win.webContents.openDevTools();
}

app.whenReady().then(createWindow).catch(err => {
    console.error("Failed to create window:", err);
});

// Quit when all windows are closed, except on macOS.
app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

app.on('activate', () => {
    // On macOS, re-create window when the dock icon is clicked and no other windows are open.
    if (BrowserWindow.getAllWindows().length === 0) {
        createWindow();
    }
});