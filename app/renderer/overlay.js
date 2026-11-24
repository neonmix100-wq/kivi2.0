'use strict';

const WS_URL = 'ws://127.0.0.1:8765';
const RECONNECT_MS = 2000;
const PILL_WIDTH = 260;
const CARD_WIDTH = 400;

const pill = document.getElementById('pill');
const pillText = document.getElementById('pill-text');
const pillConn = document.getElementById('pill-conn');
const askCard = document.getElementById('ask-card');
const askQuestion = document.getElementById('ask-question');
const askOptions = document.getElementById('ask-options');
const askInput = document.getElementById('ask-input');
const askSend = document.getElementById('ask-send');
const askSkip = document.getElementById('ask-skip');
const confirmCard = document.getElementById('confirm-card');
const confirmTitle = document.getElementById('confirm-title');
const confirmBody = document.getElementById('confirm-body');
const confirmDetails = document.getElementById('confirm-details');
const confirmOk = document.getElementById('confirm-ok');
const confirmCancel = document.getElementById('confirm-cancel');
const overlayToast = document.getElementById('overlay-toast');
const root = document.getElementById('overlay-root');

let socket = null;
let reconnectTimer = null;
let toastTimer = null;
let currentAsk = null;
let currentConfirm = null;
let resizeScheduled = false;

/* Оформление оверлея полностью задаёт theme.js/theme.css. */

function mode() {
  return (!askCard.hidden || !confirmCard.hidden) ? 'card' : 'pill';
}

function reportSize() {
  const current = mode();
  const width = current === 'card' ? CARD_WIDTH : PILL_WIDTH;
  const height = Math.max(44, Math.ceil(root.getBoundingClientRect().height) + 2);
  const api = window.kivi;
  if (api && api.send) api.send('overlay:resize', { width, height, mode: current });
}

function scheduleResize() {
  if (resizeScheduled) return;
  resizeScheduled = true;
  requestAnimationFrame(() => {
    resizeScheduled = false;
    reportSize();
  });
}

function send(payload) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(payload));
  return true;
}

function setPill(state, text, online) {
  pill.dataset.state = state;
  pillText.textContent = text;
  pill.classList.toggle('reconnecting', !online);
  pillConn.textContent = online ? 'online' : 'переподключение';
  scheduleResize();
}

function stateLabel(status) {
  const state = String(status.state || 'IDLE').toUpperCase();
  if (state === 'SPEAKING') return 'говорю';
  if (state === 'THINKING') return 'думаю';
  if (status.window_open) return 'диалог';
  return 'IDLE';
}

function handleStatus(message) {
  const label = stateLabel(message);
  const visual = label === 'говорю' ? 'speaking'
    : label === 'думаю' ? 'thinking'
      : label === 'диалог' ? 'dialog' : 'idle';
  setPill(visual, `Kivi · ${label}`, true);
}

function closeAsk(answer) {
  if (!currentAsk) return;
  send({ command: 'ask_response', id: currentAsk, answer });
  currentAsk = null;
  askCard.hidden = true;
  askOptions.replaceChildren();
  askInput.value = '';
  scheduleResize();
}

function showAsk(message) {
  confirmCard.hidden = true;
  currentConfirm = null;
  currentAsk = message.id;
  askQuestion.textContent = String(message.question || '');
  askOptions.replaceChildren();
  const options = Array.isArray(message.options) ? message.options : [];
  options.forEach((option) => {
    const button = document.createElement('button');
    button.className = 'card-btn';
    button.textContent = String(option);
    button.addEventListener('click', () => closeAsk(String(option)));
    askOptions.appendChild(button);
  });
  askCard.hidden = false;
  scheduleResize();
  setTimeout(() => askInput.focus(), 60);
}

function closeConfirm(ok) {
  if (!currentConfirm) return;
  send({ command: 'confirm_response', id: currentConfirm, ok: Boolean(ok) });
  currentConfirm = null;
  confirmCard.hidden = true;
  confirmTitle.textContent = '';
  confirmBody.textContent = '';
  confirmDetails.textContent = '';
  scheduleResize();
}

function showConfirm(message) {
  askCard.hidden = true;
  currentAsk = null;
  currentConfirm = message.id;
  confirmTitle.textContent = String(message.title || 'Подтвердить?');
  confirmBody.textContent = String(message.body || '');
  confirmDetails.textContent = String(message.details || '');
  confirmCard.hidden = false;
  scheduleResize();
}

function showToast(text) {
  overlayToast.textContent = String(text || '');
  overlayToast.hidden = false;
  scheduleResize();
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    overlayToast.hidden = true;
    scheduleResize();
  }, 4000);
}

function handleMessage(message) {
  if (!message || typeof message !== 'object') return;
  switch (message.event) {
    case 'status':
      handleStatus(message);
      break;
    case 'ask':
      showAsk(message);
      break;
    case 'confirm':
      showConfirm(message);
      break;
    case 'info':
      showToast(message.text);
      break;
    default:
      break;
  }
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, RECONNECT_MS);
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
    setPill('idle', 'Kivi · IDLE', true);
    send({ command: 'get_status' });
  });
  socket.addEventListener('message', (event) => {
    try {
      handleMessage(JSON.parse(event.data));
    } catch (_) {
      /* битый кадр игнорируем */
    }
  });
  socket.addEventListener('close', () => {
    setPill('idle', 'переподключение…', false);
    scheduleReconnect();
  });
  socket.addEventListener('error', () => {
    try { socket.close(); } catch (_) { /* ignore */ }
  });
}

askSend.addEventListener('click', () => {
  const value = askInput.value.trim();
  if (value) closeAsk(value);
});
askInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    const value = askInput.value.trim();
    if (value) closeAsk(value);
  }
});
askSkip.addEventListener('click', () => closeAsk(''));
confirmOk.addEventListener('click', () => closeConfirm(true));
confirmCancel.addEventListener('click', () => closeConfirm(false));

new ResizeObserver(() => scheduleResize()).observe(root);
setPill('idle', 'Kivi · IDLE', false);
connect();
