// ================================================
// electron/main.js (Main Process)
// This is the entry point for the native desktop application shell.
// It handles window creation and IPC communication with React.
// ================================================

const { app, BrowserWindow } = require('electron'); // <-- Use require() here
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


/**
 * Initializes the Electron app lifecycle and event listeners.
 */
function initializeApp() {
    if (!app) {
        console.error('Electron "app" module is undefined or not available.');
        return;
    }

    // 1. Attempt to use the standard, modern initialization pattern
app.whenReady().then(() => {
        createWindow();
        setupEventListeners();
}).catch(err => {
        console.error('Failed during app.whenReady() setup:', err);
        // Fallback: If whenReady fails, we proceed to set up listeners anyway
        // in case the error was transient.
        setupEventListeners();
});

    // 2. Setup event listeners (must run regardless of which path above is taken)
    function setupEventListeners() {
        app.on('window-all-closed', () => {
            if (process.platform !== 'darwin') {
                app.quit();
            }
        });

        app.on('activate', () => {
            if (BrowserWindow.getAllWindows().length === 0) {
                createWindow();
            }
        });
    }
}

// Start the application initialization process
initializeApp();

