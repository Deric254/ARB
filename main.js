const { app, BrowserWindow, ipcMain, dialog, Tray, Menu, nativeImage } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const crypto = require('crypto');
const log = require('electron-log');

log.initialize();

// Paths
const isDev = !app.isPackaged;
const RESOURCES_PATH = isDev ? __dirname : process.resourcesPath;
const BACKEND_DIR = path.join(RESOURCES_PATH, 'backend');
const FRONTEND_DIR = path.join(RESOURCES_PATH, 'frontend');
const BUILD_DIR = path.join(RESOURCES_PATH, 'build');

let mainWindow = null;
let splashWindow = null;
let tray = null;
let backendProcess = null;
let backendPort = 8765;
let backendReady = false;
// Generated fresh each launch, never written to disk. Passed to the
// backend via env var and to the renderer via IPC so every request
// can be authenticated without a login step.
const apiToken = crypto.randomBytes(32).toString('hex');

// Find backend executable
function getBackendExe() {
  const exeName = process.platform === 'win32' ? 'arb_backend.exe' : 'arb_backend';
  const candidates = [
    path.join(BACKEND_DIR, exeName),
    path.join(BACKEND_DIR, 'dist', exeName),
    path.join(RESOURCES_PATH, 'backend', exeName),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  // Fallback: try Python directly in dev mode
  const pyScript = path.join(BACKEND_DIR, 'main.py');
  if (fs.existsSync(pyScript)) return { python: true, script: pyScript };
  return null;
}

function findPython() {
  const cmds = process.platform === 'win32' 
    ? ['python.exe', 'python3.exe', 'py.exe'] 
    : ['python3', 'python'];
  for (const cmd of cmds) {
    try {
      require('child_process').execSync(`${cmd} --version`, { stdio: 'pipe' });
      return cmd;
    } catch (e) {}
  }
  return null;
}

function pollBackendReady(maxWaitMs = 30000, intervalMs = 300) {
  const start = Date.now();
  const check = () => {
    if (backendReady) return; // already transitioned (e.g. app quitting or restarted)
    const req = http.get({ host: 'localhost', port: backendPort, path: '/', timeout: 1000 }, (res) => {
      res.resume();
      if (res.statusCode === 200 && !backendReady) {
        backendReady = true;
        log.info('Backend HTTP ready on port', backendPort);
        if (splashWindow && !splashWindow.isDestroyed()) {
          splashWindow.close();
          createMainWindow();
        }
      } else if (Date.now() - start < maxWaitMs) {
        setTimeout(check, intervalMs);
      }
    });
    req.on('error', () => {
      if (Date.now() - start < maxWaitMs) {
        setTimeout(check, intervalMs);
      }
    });
    req.on('timeout', () => req.destroy());
  };
  check();
}

function startBackend() {
  const backend = getBackendExe();
  if (!backend) {
    log.error('Backend executable not found');
    dialog.showErrorBox('Backend Missing', 'Backend executable not found. Please reinstall.');
    return false;
  }

  let cmd, args;
  if (backend.python) {
    const py = findPython();
    if (!py) {
      dialog.showErrorBox('Python Required', 'Python 3.9+ is required for development mode.\nDownload from python.org');
      return false;
    }
    cmd = py;
    args = [backend.script];
    log.info('Starting backend via Python (dev mode)');
  } else {
    cmd = backend;
    args = [];
    log.info('Starting bundled backend:', backend);
  }

  backendProcess = spawn(cmd, args, {
    cwd: path.dirname(cmd),
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, ARB_PORT: backendPort, ARB_DESKTOP: '1', ARB_TOKEN: apiToken }
  });

  backendProcess.stdout.on('data', (data) => {
    const line = data.toString().trim();
    log.info('[PY]', line);
  });

  backendProcess.stderr.on('data', (data) => {
    log.error('[PY-ERR]', data.toString().trim());
  });

  backendProcess.on('close', (code) => {
    log.warn('Backend exited with code', code);
    backendReady = false;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend-disconnected');
    }
  });

  backendProcess.on('error', (err) => {
    log.error('Backend process error:', err);
  });

  backendReady = false;
  pollBackendReady();

  return true;
}

function createSplashWindow() {
  splashWindow = new BrowserWindow({
    width: 500,
    height: 350,
    frame: false,
    alwaysOnTop: true,
    transparent: true,
    resizable: false,
    icon: getIcon(),
    webPreferences: { nodeIntegration: false, contextIsolation: true }
  });

  splashWindow.loadFile(path.join(FRONTEND_DIR, 'splash.html'));
  splashWindow.center();
}

function createMainWindow() {
  if (mainWindow) {
    mainWindow.focus();
    return;
  }

  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1200,
    minHeight: 700,
    title: 'ARB Pro v6.0',
    icon: getIcon(),
    show: false,
    backgroundColor: '#0a0a0a',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: false
    }
  });

  mainWindow.loadFile(path.join(FRONTEND_DIR, 'index.html'));

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    mainWindow.maximize();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  mainWindow.on('minimize', () => {
    if (process.platform === 'win32' && tray) {
      mainWindow.hide();
    }
  });
}

function showMainWindowAndGoToSettings() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createMainWindow();
  }
  mainWindow.show();
  mainWindow.focus();
  mainWindow.webContents.send('navigate-settings');
}

function getIcon() {
  const iconPath = process.platform === 'win32' 
    ? path.join(BUILD_DIR, 'icon.ico')
    : path.join(BUILD_DIR, 'icon.png');
  if (fs.existsSync(iconPath)) {
    return nativeImage.createFromPath(iconPath);
  }
  return null;
}

function createTray() {
  const iconPath = path.join(BUILD_DIR, 'icon.png');
  if (!fs.existsSync(iconPath)) return;

  tray = new Tray(nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 }));
  const contextMenu = Menu.buildFromTemplate([
    { label: 'Show ARB Pro', click: () => { if (mainWindow) mainWindow.show(); } },
    { label: 'Settings', click: showMainWindowAndGoToSettings },
    { type: 'separator' },
    { label: 'Quit', click: () => { app.quit(); } }
  ]);
  tray.setToolTip('ARB Pro v6.0');
  tray.setContextMenu(contextMenu);
  tray.on('click', () => {
    if (mainWindow) {
      mainWindow.isVisible() ? mainWindow.hide() : mainWindow.show();
    }
  });
}

// App lifecycle
app.whenReady().then(() => {
  createSplashWindow();
  if (!startBackend()) {
    setTimeout(() => app.quit(), 3000);
    return;
  }

  // Timeout: if backend doesn't start in 30s, show error
  setTimeout(() => {
    if (!backendReady && splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.close();
      dialog.showErrorBox('Startup Error', 'Backend failed to start. Check logs.');
      app.quit();
    }
  }, 30000);

  createTray();
});

app.on('window-all-closed', () => {
  if (backendProcess) {
    backendProcess.kill();
    backendProcess = null;
  }
  if (tray) tray.destroy();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  if (backendProcess) {
    backendProcess.kill();
    backendProcess = null;
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
});

// IPC handlers
ipcMain.handle('get-app-version', () => app.getVersion());
ipcMain.handle('select-logo', async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openFile'],
    filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'svg'] }]
  });
  return result.filePaths[0] || null;
});
ipcMain.handle('get-backend-status', () => ({ ready: backendReady, port: backendPort }));
ipcMain.handle('get-api-token', () => apiToken);
ipcMain.handle('restart-backend', async () => {
  if (backendProcess) backendProcess.kill();
  backendReady = false;
  await new Promise(r => setTimeout(r, 1000));
  return startBackend();
});
