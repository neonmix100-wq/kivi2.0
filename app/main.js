'use strict';

const { app, BrowserWindow, ipcMain, screen, Menu } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const net = require('node:net');
const { spawn, execFile } = require('node:child_process');

const UI_ROOT = __dirname;
const DEFAULT_KIVI_HOME = 'D:\\Kivi';
const CORE_HOST = '127.0.0.1';
const CORE_PORT = 8765;
const OVERLAY_MARGIN = 16;
const PILL_SIZE = { width: 260, height: 48 };
const CARD_MAX = { width: 460, height: 420 };

let mainWindow = null;
let overlayWindow = null;
let coreChild = null;
let coreSpawned = false;
let appLogPath = null;
let mainWindowTransparent = false;

function writeAppLog(...args) {
  const line = args
    .map((value) => (typeof value === 'string' ? value : JSON.stringify(value)))
    .join(' ');
  console.log(line);
  if (!appLogPath) return;
  try {
    fs.mkdirSync(path.dirname(appLogPath), { recursive: true });
    fs.appendFileSync(appLogPath, `${new Date().toISOString()} ${line}\n`, 'utf8');
  } catch (_) {
    /* лог не должен ронять приложение */
  }
}

function initAppLog() {
  if (appLogPath) return;
  appLogPath = path.join(app.getPath('userData'), 'kivi-app.log');
  writeAppLog('Kivi app started', {
    packaged: app.isPackaged,
    version: app.getVersion(),
    userData: app.getPath('userData'),
  });
}

function readJsonKiviHome() {
  const file = path.join(app.getPath('userData'), 'kivi-home.json');
  if (!fs.existsSync(file)) return '';
  try {
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    const value = typeof raw === 'string' ? raw : raw && raw.kiviHome;
    return typeof value === 'string' ? value.trim() : '';
  } catch (error) {
    writeAppLog('kivi-home.json is invalid:', error.message);
    return '';
  }
}

function resolveKiviHome() {
  const envHome = String(process.env.KIVI_HOME || '').trim();
  if (envHome) return path.resolve(envHome);
  const jsonHome = readJsonKiviHome();
  if (jsonHome) return path.resolve(jsonHome);
  return DEFAULT_KIVI_HOME;
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

function coreIsReachable(timeoutMs = 700) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: CORE_HOST, port: CORE_PORT });
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      try { socket.destroy(); } catch (_) { /* ignore */ }
      resolve(value);
    };
    socket.setTimeout(timeoutMs);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
  });
}

async function ensureCore() {
  const kiviHome = resolveKiviHome();
  writeAppLog('Kivi core home:', kiviHome);
  if (await coreIsReachable()) {
    coreSpawned = false;
    writeAppLog(`Kivi core already running on ${CORE_PORT} - attaching`);
    return;
  }
  const python = path.join(kiviHome, '.venv', 'Scripts', 'python.exe');
  if (!fs.existsSync(python)) {
    writeAppLog('Kivi core python not found:', python);
    return;
  }
  coreChild = spawn(python, ['-m', 'kivi.main'], {
    cwd: kiviHome,
    env: { ...process.env, KIVI_HOME: kiviHome },
    stdio: 'ignore',
    windowsHide: true,
  });
  coreSpawned = true;
  coreChild.on('error', (error) => {
    writeAppLog('Kivi core spawn error', error.message);
    coreChild = null;
    coreSpawned = false;
  });
  coreChild.on('exit', (code, signal) => {
    writeAppLog('Kivi core exited', String(code), String(signal));
    coreChild = null;
    coreSpawned = false;
  });
  writeAppLog('Kivi core spawned pid=', coreChild.pid);
}

function stopCore() {
  if (!coreSpawned || !coreChild) return;
  const pid = coreChild.pid;
  try { coreChild.kill(); } catch (_) { /* ignore */ }
  if (process.platform === 'win32' && pid) {
    setTimeout(() => {
      try {
        execFile(
          'taskkill',
          ['/PID', String(pid), '/T', '/F'],
          { windowsHide: true },
          () => {},
        );
      } catch (_) { /* ignore */ }
    }, 1200);
  }
  coreChild = null;
  coreSpawned = false;
}

function primaryWorkArea() {
  return screen.getPrimaryDisplay().workArea;
}

function placeOverlay(width, height) {
  if (!overlayWindow) return;
  const area = primaryWorkArea();
  const x = Math.round(area.x + area.width - width - OVERLAY_MARGIN);
  const y = Math.round(area.y + area.height - height - OVERLAY_MARGIN);
  overlayWindow.setBounds({ x, y, width, height });
}

function createMainWindow() {
  const options = {
    width: 1160,
    height: 760,
    minWidth: 900,
    minHeight: 600,
    frame: false,
    show: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: false,
    title: 'Kivi',
    webPreferences: {
      preload: path.join(UI_ROOT, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: !app.isPackaged,
    },
  };
  try {
    mainWindow = new BrowserWindow(options);
    mainWindowTransparent = true;
  } catch (error) {
    writeAppLog('Kivi transparent window unavailable:', error.message);
    const fallback = { ...options, backgroundColor: '#171b22' };
    delete fallback.transparent;
    delete fallback.hasShadow;
    mainWindow = new BrowserWindow(fallback);
    mainWindowTransparent = false;
  }
  mainWindow.loadFile(path.join(UI_ROOT, 'renderer', 'index.html')).then(
    () => writeAppLog('Kivi main window loaded'),
    (error) => writeAppLog('Kivi main window load error', error.message),
  );
  mainWindow.webContents.on('did-fail-load', (_event, code, description) => {
    writeAppLog('Kivi main did-fail-load', String(code), String(description));
  });
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    writeAppLog('Kivi main render-process-gone', JSON.stringify(details));
  });
  mainWindow.once('ready-to-show', () => {
    writeAppLog('Kivi main ready-to-show');
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
  setTimeout(() => {
    if (mainWindow && !mainWindow.isVisible()) {
      writeAppLog('Kivi main force-show timeout');
      mainWindow.show();
      mainWindow.focus();
    }
  }, 4000);
  mainWindow.on('show', () => writeAppLog('Kivi main shown'));
  mainWindow.on('closed', () => {
    mainWindow = null;
    app.quit();
  });
  mainWindow.on('maximize', () => {
    if (mainWindow) mainWindow.webContents.send('window:state', { maximized: true });
  });
  mainWindow.on('unmaximize', () => {
    if (mainWindow) mainWindow.webContents.send('window:state', { maximized: false });
  });
}

function createOverlayWindow() {
  const area = primaryWorkArea();
  const width = PILL_SIZE.width;
  const height = PILL_SIZE.height;
  const x = Math.round(area.x + area.width - width - OVERLAY_MARGIN);
  const y = Math.round(area.y + area.height - height - OVERLAY_MARGIN);

  overlayWindow = new BrowserWindow({
    width,
    height,
    x,
    y,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    show: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(UI_ROOT, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: !app.isPackaged,
    },
  });
  overlayWindow.setAlwaysOnTop(true, 'screen-saver');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlayWindow.loadFile(path.join(UI_ROOT, 'renderer', 'overlay.html'));
  overlayWindow.once('ready-to-show', () => {
    if (!overlayWindow) return;
    overlayWindow.showInactive();
    overlayWindow.setIgnoreMouseEvents(true, { forward: true });
  });
  overlayWindow.on('closed', () => { overlayWindow = null; });
}

ipcMain.on('window:minimize', () => { if (mainWindow) mainWindow.minimize(); });
ipcMain.on('window:maximize', () => {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.on('window:close', () => { if (mainWindow) mainWindow.close(); });
ipcMain.on('app:quit', () => app.quit());
ipcMain.on("theme-alpha", (_event, payload) => {
  if (!mainWindow || mainWindowTransparent) return;
  const value = Math.min(1, Math.max(0.3, Number(payload && payload.alpha) || 1));
  try { mainWindow.setOpacity(value); } catch (_) { /* ignore */ }
});

ipcMain.on('overlay:resize', (_event, payload) => {
  if (!overlayWindow || !payload) return;
  const mode = payload.mode === 'card' ? 'card' : 'pill';
  const width = Math.max(
    PILL_SIZE.width,
    Math.min(CARD_MAX.width, Math.round(Number(payload.width) || PILL_SIZE.width)),
  );
  const height = Math.max(
    PILL_SIZE.height,
    Math.min(CARD_MAX.height, Math.round(Number(payload.height) || PILL_SIZE.height)),
  );
  placeOverlay(width, height);
  overlayWindow.setIgnoreMouseEvents(mode === 'pill', { forward: true });
  if (mode === 'card') overlayWindow.focus();
});

app.whenReady().then(() => {
  if (!gotLock) {
    app.quit();
    return;
  }
  initAppLog();
  if (app.isPackaged) Menu.setApplicationMenu(null);
  createMainWindow();
  createOverlayWindow();
  ensureCore().catch((error) => writeAppLog('core start failed', error.message));
});

app.on('before-quit', () => {
  writeAppLog('Kivi app quitting');
  stopCore();
});

app.on('window-all-closed', () => {
  app.quit();
});
