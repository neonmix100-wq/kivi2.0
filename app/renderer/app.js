'use strict';

const WS_URL = 'ws://127.0.0.1:8765';
const RECONNECT_MS = 2000;
const STORE_THEME = 'kivi.theme';
const STORE_ACCENT = 'kivi.accent';
const STORE_FONT = 'kivi.font';

const FONTS = {
  system: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
  tahoma: 'Tahoma, "Segoe UI", sans-serif',
  verdana: 'Verdana, Geneva, sans-serif',
  georgia: 'Georgia, "Times New Roman", serif',
};

const STATE_LABELS = {
  IDLE: 'ожидание',
  THINKING: 'думаю',
  SPEAKING: 'говорю',
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const ui = {
  connPill: $('#conn-pill'),
  connText: $('#conn-text'),
  agentState: $('#agent-state'),
  agentPulse: $('#agent-pulse'),
  statState: $('#stat-state'),
  statHint: $('#stat-state-hint'),
  statBudget: $('#stat-budget'),
  windowBadge: $('#window-badge'),
  volumeSlider: $('#volume-slider'),
  volumeValue: $('#volume-value'),
  lastEvent: $('#last-event'),
  lastEventTime: $('#last-event-time'),
  logView: $('#log-view'),
  toast: $('#toast'),
  customAccent: $('#custom-accent'),
  customPreview: $('#custom-preview'),
};

let socket = null;
let reconnectTimer = null;
let pendingRequest = null;
let volumeDragging = false;
let toastTimer = null;

/* --- тема и шрифт --- */

function hexToRgba(hex, alpha) {
  const clean = String(hex).replace('#', '');
  const value = clean.length === 3
    ? clean.split('').map((ch) => ch + ch).join('')
    : clean.padEnd(6, '0').slice(0, 6);
  const r = parseInt(value.slice(0, 2), 16) || 0;
  const g = parseInt(value.slice(2, 4), 16) || 0;
  const b = parseInt(value.slice(4, 6), 16) || 0;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function applyTheme(theme, accent) {
  document.documentElement.dataset.theme = theme;
  const rootStyle = document.documentElement.style;
  if (theme === 'custom' && accent) {
    rootStyle.setProperty('--accent', accent);
    rootStyle.setProperty('--accent-soft', hexToRgba(accent, 0.16));
    rootStyle.setProperty('--glow', hexToRgba(accent, 0.2));
    if (ui.customPreview) ui.customPreview.style.setProperty('--preview-accent', accent);
  } else {
    rootStyle.removeProperty('--accent');
    rootStyle.removeProperty('--accent-soft');
    rootStyle.removeProperty('--glow');
  }
  $$('.theme-tile').forEach((tile) => {
    tile.classList.toggle('active', tile.dataset.theme === theme);
  });
}

function applyFont(font) {
  document.documentElement.style.setProperty('--font-ui', FONTS[font] || FONTS.system);
  $$('.font-tile').forEach((tile) => {
    tile.classList.toggle('active', tile.dataset.font === font);
  });
}

function initSettings() {
  const theme = localStorage.getItem(STORE_THEME) || 'dark';
  const accent = localStorage.getItem(STORE_ACCENT) || '#f0703c';
  const font = localStorage.getItem(STORE_FONT) || 'system';
  ui.customAccent.value = accent;
  applyTheme(theme, accent);
  applyFont(font);

  $$('.theme-tile').forEach((tile) => {
    tile.addEventListener('click', (event) => {
      if (event.target === ui.customAccent) return;
      const next = tile.dataset.theme;
      if (next === 'custom') {
        const value = ui.customAccent.value || accent;
        localStorage.setItem(STORE_ACCENT, value);
        localStorage.setItem(STORE_THEME, 'custom');
        applyTheme('custom', value);
      } else {
        localStorage.setItem(STORE_THEME, next);
        applyTheme(next, accent);
      }
    });
  });

  ui.customAccent.addEventListener('input', () => {
    const value = ui.customAccent.value;
    localStorage.setItem(STORE_ACCENT, value);
    localStorage.setItem(STORE_THEME, 'custom');
    applyTheme('custom', value);
  });

  $$('.font-tile').forEach((tile) => {
    tile.addEventListener('click', () => {
      const selected = tile.dataset.font;
      localStorage.setItem(STORE_FONT, selected);
      applyFont(selected);
    });
  });
}

/* --- WS --- */

function setConnection(online) {
  if (ui.connPill) {
    ui.connPill.classList.toggle('online', online);
    ui.connPill.classList.toggle('offline', !online);
  }
  if (ui.connText) ui.connText.textContent = online ? 'ядро на связи' : 'переподключение…';
}

function request(command, fields = {}, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      reject(new Error('нет соединения'));
      return;
    }
    if (pendingRequest) pendingRequest.reject(new Error('запрос уже идёт'));
    const timer = setTimeout(() => {
      if (pendingRequest && pendingRequest.timer === timer) pendingRequest = null;
      reject(new Error('таймаут'));
    }, timeoutMs);
    pendingRequest = { resolve, reject, timer };
    socket.send(JSON.stringify({ command, ...fields }));
  });
}

function showToast(text) {
  if (!ui.toast) return;
  ui.toast.textContent = text;
  ui.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 3500);
}

function noteEvent(text) {
  if (ui.lastEvent) ui.lastEvent.textContent = text;
  if (ui.lastEventTime) {
    ui.lastEventTime.textContent = new Date().toLocaleTimeString('ru-RU');
  }
}

function appendLog(line) {
  if (!ui.logView) return;
  const view = ui.logView;
  const nearBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 40;
  view.textContent += (view.textContent ? '\n' : '') + line;
  const lines = view.textContent.split('\n');
  if (lines.length > 400) view.textContent = lines.slice(-400).join('\n');
  if (nearBottom) view.scrollTop = view.scrollHeight;
}

function updateVolumeLabel(percent) {
  if (ui.volumeValue) ui.volumeValue.textContent = `${percent}%`;
  if (ui.volumeSlider) {
    ui.volumeSlider.style.background =
      `linear-gradient(90deg, var(--accent) 0%, var(--accent) ${percent}%, var(--card-2) ${percent}%)`;
  }
}

function updateStatus(status) {
  const state = String(status.state || 'IDLE').toUpperCase();
  if (ui.statState) ui.statState.textContent = STATE_LABELS[state] || state;
  if (ui.statHint) {
    ui.statHint.textContent = status.window_open ? 'окно диалога открыто' : 'окно диалога закрыто';
  }
  if (ui.agentState) ui.agentState.textContent = state;
  if (ui.agentPulse) ui.agentPulse.classList.toggle('active', state !== 'IDLE');
  if (ui.statBudget && status.budget_used !== undefined) {
    ui.statBudget.textContent = Number(status.budget_used || 0).toFixed(2);
  }
  if (ui.windowBadge) {
    const isOpen = Boolean(status.window_open);
    ui.windowBadge.textContent = isOpen ? 'окно: открыто' : 'окно: закрыто';
    ui.windowBadge.classList.toggle('open', isOpen);
  }
  if (ui.volumeSlider && status.volume !== undefined && !volumeDragging) {
    const percent = Math.round(Number(status.volume) * 100);
    ui.volumeSlider.value = String(percent);
    updateVolumeLabel(percent);
  }
}

function handleMessage(message) {
  if (!message || typeof message !== 'object') return;
  if ('ok' in message) {
    if (pendingRequest) {
      const pending = pendingRequest;
      pendingRequest = null;
      clearTimeout(pending.timer);
      pending.resolve(message);
    }
    return;
  }
  switch (message.event) {
    case 'status':
      updateStatus(message);
      noteEvent(`status: ${message.state}${message.window_open ? ' · окно открыто' : ''}`);
      break;
    case 'log':
      appendLog(String(message.line || ''));
      break;
    case 'info':
      showToast(String(message.text || ''));
      noteEvent(`info: ${message.text || ''}`);
      break;
    default:
      break;
  }
}

function connect() {
  clearTimeout(reconnectTimer);
  try {
    socket = new WebSocket(WS_URL);
  } catch (_) {
    scheduleReconnect();
    return;
  }
  socket.addEventListener('open', () => {
    setConnection(true);
    request('get_status').catch(() => {});
    loadLogs();
  });
  socket.addEventListener('message', (event) => {
    let message = null;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    handleMessage(message);
  });
  socket.addEventListener('close', () => {
    setConnection(false);
    scheduleReconnect();
  });
  socket.addEventListener('error', () => {
    try { socket.close(); } catch (_) { /* ignore */ }
  });
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, RECONNECT_MS);
}

function loadLogs() {
  request('tail_logs', { n: 200 }).then((reply) => {
    if (!ui.logView) return;
    const lines = Array.isArray(reply.lines) ? reply.lines : [];
    ui.logView.textContent = lines.join('\n');
    ui.logView.scrollTop = ui.logView.scrollHeight;
  }).catch(() => {});
}

/* --- инициализация интерфейса --- */

function initNav() {
  $$('.nav-item').forEach((item) => {
    item.addEventListener('click', () => {
      $$('.nav-item').forEach((other) => other.classList.remove('active'));
      item.classList.add('active');
      const page = item.dataset.page;
      $$('.page').forEach((section) => {
        section.classList.toggle('active', section.id === `page-${page}`);
      });
      if (page === 'logs') loadLogs();
    });
  });
  const refresh = $('#btn-refresh-logs');
  if (refresh) refresh.addEventListener('click', loadLogs);
}

function initWindowControls() {
  const api = window.kivi;
  const bind = (selector, channel) => {
    const button = $(selector);
    if (!button) return;
    button.addEventListener('click', () => {
      if (api && api.send) api.send(channel);
    });
  };
  bind('#btn-min', 'window:minimize');
  bind('#btn-max', 'window:maximize');
  bind('#btn-close', 'window:close');
  if (api && api.on) {
    api.on('window:state', (data) => {
      const button = $('#btn-max');
      if (button) button.textContent = data && data.maximized ? '❐' : '□';
    });
  }
}

function initVolume() {
  if (!ui.volumeSlider) return;
  updateVolumeLabel(Number(ui.volumeSlider.value));
  ui.volumeSlider.addEventListener('input', () => {
    volumeDragging = true;
    updateVolumeLabel(Number(ui.volumeSlider.value));
  });
  ui.volumeSlider.addEventListener('change', () => {
    const percent = Number(ui.volumeSlider.value);
    volumeDragging = false;
    updateVolumeLabel(percent);
    request('set_volume', { v: percent / 100 })
      .then((reply) => {
        if (reply && reply.volume !== undefined) {
          const actual = Math.round(Number(reply.volume) * 100);
          ui.volumeSlider.value = String(actual);
          updateVolumeLabel(actual);
          noteEvent(`громкость: ${actual}%`);
        }
      })
      .catch(() => showToast('не удалось изменить громкость'));
  });
}

function init() {
  initSettings();
  initNav();
  initWindowControls();
  initVolume();
  setConnection(false);
  connect();
}

init();
