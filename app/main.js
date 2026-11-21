'use strict';

const { app, BrowserWindow, ipcMain, screen } = require('electron');
const path = require('node:path');
const net = require('node:net');
const { spawn, execFile } = require('node:child_process');

const PROJECT_ROOT = path.resolve(__dirname, '..');
const CORE_HOST = '127.0.0.1';
const CORE_PORT = 8765;
const OVERLAY_MARGIN = 16;
const PILL_SIZE = { width: 260, height: 48 };
const CARD_MAX = { width: 460, height: 420 };

let mainWindow = null;
let overlayWindow = null;
let coreChild = null;
let coreSpawned = false;

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
  if (await coreIsReachable()) {
    coreSpawned = false;
    console.log('Kivi core already running on', CORE_PORT, '- attaching');
    return;
  }
  const python = path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe');
  coreChild = spawn(python, ['-m', 'kivi.main'], {
    cwd: PROJECT_ROOT,
    env: process.env,
    stdio: 'ignore',
    windowsHide: true,
  });
  coreSpawned = true;
  coreChild.on('error', (error) => {
    console.error('Kivi core spawn error', error);
    coreChild = null;
    coreSpawned = false;
  });
  coreChild.on('exit', (code, signal) => {
    console.log('Kivi core exited', code, signal);
    coreChild = null;
    coreSpawned = false;
  });
  console.log('Kivi core spawned pid=', coreChild.pid);
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
  mainWindow = new BrowserWindow({
    width: 1160,
    height: 760,
    minWidth: 900,
    minHeight: 600,
    frame: false,
    show: false,
    backgroundColor: '#171b22',
    title: 'Kivi',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
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
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  overlayWindow.setAlwaysOnTop(true, 'screen-saver');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlayWindow.loadFile(path.join(__dirname, 'renderer', 'overlay.html'));
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
  createMainWindow();
  createOverlayWindow();
  ensureCore().catch((error) => console.error('core start failed', error));
});

app.on('before-quit', () => {
  stopCore();
});

app.on('window-all-closed', () => {
  app.quit();
});
