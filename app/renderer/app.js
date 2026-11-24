'use strict';

const WS_URL = 'ws://127.0.0.1:8765';
const RECONNECT_MS = 2000;

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
  screenLook: $('#btn-screen-look'),
  lastEvent: $('#last-event'),
  lastEventTime: $('#last-event-time'),
  logView: $('#log-view'),
  toast: $('#toast'),
  memoryList: $('#memory-list'),
  memoryState: $('#memory-state'),
  remindersView: $('#reminders-view'),
  skillsList: $('#skills-list'),
  skillsState: $('#skills-state'),
  gapsBody: $('#gaps-body'),
  gapsState: $('#gaps-state'),
  scheduleForm: $('#schedule-form'),
  scheduleWhen: $('#schedule-when'),
  scheduleWhat: $('#schedule-what'),
  scheduleAdd: $('#btn-schedule-add'),
  chatFeed: $('#chat-feed'),
  chatTyping: $('#chat-typing'),
  chatForm: $('#chat-form'),
  chatInput: $('#chat-input'),
  chatState: $('#chat-state'),
  testsList: $('#tests-list'),
  testsState: $('#tests-state'),
  testsCaption: $('#tests-title'),
  testsBadge: $('#tests-badge'),
  testsOutput: $('#tests-output'),
  testsRefresh: $('#btn-tests-refresh'),
  testsRunAll: $('#btn-tests-run-all'),
};

let socket = null;
let reconnectTimer = null;
let pendingRequest = null;
let volumeDragging = false;
let toastTimer = null;
let memoryKind = 'profile';
let testsKnown = [];
let testsRunning = false;
let currentTest = null;
let testOutputFor = null;
let testsQueue = [];

/* --- тема оформляется в theme.js/theme.css --- */


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
  if (!message.event && 'ok' in message) {
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
    case 'turn':
      addChatTurn(message);
      if (message.role === 'kivi') {
        setChatTyping(false);
        setChatState(`маршрут: ${message.route || 'chat'}`);
      }
      noteEvent(`turn: ${message.role} · ${message.source || ''}`);
      break;
    case 'test_line':
      onTestLine(message);
      break;
    case 'test_done':
      onTestDone(message);
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
    const settingsPage = $('#page-settings');
    if (settingsPage && settingsPage.classList.contains('active')) loadTests();
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

/* --- память --- */

function memoryReplyLines(reply) {
  if (Array.isArray(reply.lines)) return reply.lines.map((line) => String(line));
  if (Array.isArray(reply.items)) {
    return reply.items.map((item) => String(item.text || item.name || item));
  }
  return [];
}

function setMemoryState(text) {
  if (ui.memoryState) ui.memoryState.textContent = text;
}

function renderReminders(items) {
  if (!ui.remindersView) return;
  ui.remindersView.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'muted';
    empty.textContent = 'Напоминаний нет.';
    ui.remindersView.append(empty);
    return;
  }
  items.forEach((item) => {
    const row = document.createElement('div');
    row.className = 'reminder-row';
    const main = document.createElement('div');
    main.className = 'reminder-main';
    const when = document.createElement('strong');
    when.textContent = item.when_label || item.ts || '—';
    const text = document.createElement('span');
    text.textContent = item.what || '—';
    main.append(when, text);
    const status = document.createElement('span');
    status.className = `reminder-status ${item.done ? 'done' : 'pending'}`;
    status.textContent = item.done ? 'выполнено' : 'ожидает';
    const cancel = document.createElement('button');
    cancel.className = 'ghost-btn reminder-cancel';
    cancel.textContent = 'Отменить';
    cancel.disabled = Boolean(item.done);
    cancel.addEventListener('click', () => {
      request('cancel_reminder', { id: item.id })
        .then((reply) => {
          setMemoryState(reply.ok ? 'напоминание отменено' : 'не удалось отменить');
          loadReminders();
        })
        .catch(() => setMemoryState('нет связи с ядром'));
    });
    row.append(main, status, cancel);
    ui.remindersView.append(row);
  });
}

function loadReminders() {
  if (!ui.remindersView) return;
  ui.remindersView.textContent = 'Загружаю…';
  setMemoryState('');
  request('reminders_list')
    .then((reply) => {
      const items = Array.isArray(reply.reminders) ? reply.reminders : [];
      renderReminders(items);
      const pending = items.filter((item) => !item.done).length;
      setMemoryState(`напоминаний: ${items.length}, ожидают: ${pending}`);
    })
    .catch((error) => {
      ui.remindersView.textContent = 'Нет связи с ядром.';
      setMemoryState(`ошибка: ${error.message || error}`);
    });
}

function loadMemory(kind = memoryKind) {
  memoryKind = kind || 'profile';
  $$('.memory-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.kind === memoryKind);
  });
  if (ui.scheduleForm && memoryKind !== 'schedule') ui.scheduleForm.hidden = true;
  if (ui.scheduleAdd) ui.scheduleAdd.hidden = memoryKind !== 'schedule';
  if (memoryKind === 'reminders') {
    if (ui.memoryList) ui.memoryList.hidden = true;
    if (ui.remindersView) ui.remindersView.hidden = false;
    loadReminders();
    return;
  }
  if (ui.memoryList) ui.memoryList.hidden = false;
  if (ui.remindersView) ui.remindersView.hidden = true;
  if (!ui.memoryList) return;
  ui.memoryList.textContent = 'Загружаю…';
  setMemoryState('');
  request('get_memory', { kind: memoryKind })
    .then((reply) => {
      const lines = memoryReplyLines(reply);
      ui.memoryList.textContent = lines.length
        ? lines.join('\n')
        : 'Пока пусто.';
      setMemoryState(`вид: ${reply.kind || memoryKind} · строк: ${lines.length}`);
    })
    .catch((error) => {
      ui.memoryList.textContent = 'Нет связи с ядром.';
      setMemoryState(`ошибка: ${error.message || error}`);
    });
}

function reflectMemoryNow() {
  setMemoryState('Извлекаю память…');
  request('reflect_now', {}, 90000)
    .then((reply) => {
      if (reply.ok === false) {
        setMemoryState(`не удалось: ${reply.error || 'ошибка'}`);
        return;
      }
      const stats = reply.stats || {};
      const summary = [
        `фактов ${stats.facts ?? 0}`,
        `расписание ${stats.schedule ?? 0}`,
        `задач ${stats.tasks ?? 0}`,
        `предпочтений ${stats.preferences ?? 0}`,
      ].join(' · ');
      setMemoryState(`готово: ${summary}`);
      loadMemory(memoryKind);
    })
    .catch((error) => setMemoryState(`не удалось: ${error.message || error}`));
}

function initMemory() {
  $$('.memory-tab').forEach((tab) => {
    tab.addEventListener('click', () => loadMemory(tab.dataset.kind));
  });
  const refresh = $('#btn-memory-refresh');
  if (refresh) refresh.addEventListener('click', () => loadMemory(memoryKind));
  const reflect = $('#btn-memory-reflect');
  if (reflect) reflect.addEventListener('click', reflectMemoryNow);
}

/* --- скиллы --- */

function skillStat(label, value) {
  const item = document.createElement('span');
  item.className = 'skill-stat';
  item.textContent = `${label}: ${value}`;
  return item;
}

function renderSkills(skills) {
  if (!ui.skillsList) return;
  ui.skillsList.replaceChildren();
  if (!skills.length) {
    const empty = document.createElement('div');
    empty.className = 'muted';
    empty.textContent = 'Скиллы не найдены.';
    ui.skillsList.append(empty);
    return;
  }
  skills.forEach((skill) => {
    const row = document.createElement('div');
    row.className = 'skill-row';

    const main = document.createElement('div');
    main.className = 'skill-main';
    const title = document.createElement('div');
    title.className = 'skill-title';
    title.textContent = skill.name;
    const description = document.createElement('div');
    description.className = 'skill-description';
    description.textContent = skill.description || '—';
    main.append(title, description);

    const stats = document.createElement('div');
    stats.className = 'skill-stats';
    stats.append(
      skillStat('вызовов', skill.use_count || 0),
      skillStat('успешно', skill.success_count || 0),
      skillStat('ошибок', skill.fail_count || 0),
    );

    const controls = document.createElement('div');
    controls.className = 'skill-controls';
    const toggle = document.createElement('label');
    toggle.className = 'skill-toggle';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = Boolean(skill.enabled);
    checkbox.addEventListener('change', () => {
      request('set_skill_enabled', { name: skill.name, enabled: checkbox.checked })
        .then((reply) => {
          if (reply.ok === false) {
            checkbox.checked = !checkbox.checked;
            showToast(`не удалось переключить ${skill.name}`);
            return;
          }
          setSkillsState(`${skill.name}: ${checkbox.checked ? 'включён' : 'выключен'}`);
        })
        .catch(() => {
          checkbox.checked = !checkbox.checked;
          showToast('нет связи с ядром');
        });
    });
    const toggleText = document.createElement('span');
    toggleText.textContent = 'вкл';
    toggle.append(checkbox, toggleText);

    const testButton = document.createElement('button');
    testButton.className = 'ghost-btn skill-test';
    testButton.textContent = 'Протестировать';
    testButton.addEventListener('click', () => {
      request('test_skill', { name: skill.name })
        .then((reply) => {
          showToast(reply.message || reply.error || 'тест завершён');
        })
        .catch(() => showToast('нет связи с ядром'));
    });
    controls.append(toggle, testButton);
    row.append(main, stats, controls);
    ui.skillsList.append(row);
  });
}

function setSkillsState(text) {
  if (ui.skillsState) ui.skillsState.textContent = text;
}

function loadSkills() {
  if (!ui.skillsList) return;
  ui.skillsList.textContent = 'Загружаю…';
  setSkillsState('');
  request('get_skills')
    .then((reply) => {
      const skills = Array.isArray(reply.skills) ? reply.skills : [];
      renderSkills(skills);
      setSkillsState(`скиллов: ${skills.length}`);
    })
    .catch((error) => {
      ui.skillsList.textContent = 'Нет связи с ядром.';
      setSkillsState(`ошибка: ${error.message || error}`);
    });
}

function initSkills() {
  const refresh = $('#btn-skills-refresh');
  if (refresh) refresh.addEventListener('click', loadSkills);
}

/* --- настройки: запуск smoke-тестов --- */

function setTestsState(text) {
  if (ui.testsState) ui.testsState.textContent = text;
}

function setTestBadge(ok) {
  if (!ui.testsBadge) return;
  if (ok === null || ok === undefined) {
    ui.testsBadge.hidden = true;
    ui.testsBadge.textContent = '';
    ui.testsBadge.classList.remove('pass', 'fail', 'run');
    return;
  }
  ui.testsBadge.hidden = false;
  ui.testsBadge.textContent = ok ? 'PASS' : 'FAIL';
  ui.testsBadge.classList.remove('run');
  ui.testsBadge.classList.toggle('pass', Boolean(ok));
  ui.testsBadge.classList.toggle('fail', !ok);
}

function setTestCaption(name) {
  if (!ui.testsCaption) return;
  ui.testsCaption.textContent = name ? `Вывод: ${name}` : 'Вывод: —';
}

function startTestUI(name) {
  currentTest = name;
  testOutputFor = name;
  const out = document.getElementById('tests-output');
  if (out) out.textContent = '';
  const badge = document.getElementById('tests-badge');
  if (badge) {
    badge.hidden = false;
    badge.textContent = '…';
    badge.className = 'test-badge run';
  }
  const title = document.getElementById('tests-title');
  if (title) title.textContent = 'Вывод: ' + name;
}

function beginTestOutput(name) {
  currentTest = name;
  if (testOutputFor === name) return;
  startTestUI(name);
}

function onTestLine(msg) {
  if (!currentTest) startTestUI(msg.name);
  if (msg.name !== currentTest) return;
  if (testOutputFor !== msg.name) startTestUI(msg.name);
  appendTestOutput(String(msg.line || ''));
}

function appendTestOutput(line) {
  if (!ui.testsOutput) return;
  const view = ui.testsOutput;
  const nearBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 40;
  view.textContent += (view.textContent ? '\n' : '') + String(line);
  const lines = view.textContent.split('\n');
  if (lines.length > 2000) view.textContent = lines.slice(-2000).join('\n');
  if (nearBottom) view.scrollTop = view.scrollHeight;
}

function setTestsBusy(busy) {
  testsRunning = Boolean(busy);
  if (ui.testsRunAll) ui.testsRunAll.disabled = testsRunning;
  if (ui.testsRefresh) ui.testsRefresh.disabled = testsRunning;
  $$('.test-run').forEach((button) => {
    button.disabled = testsRunning;
  });
}

function renderTests(names) {
  if (!ui.testsList) return;
  ui.testsList.replaceChildren();
  if (!names.length) {
    const empty = document.createElement('div');
    empty.className = 'muted';
    empty.textContent = 'Файлы tests/smoke_*.py не найдены.';
    ui.testsList.append(empty);
    return;
  }
  names.forEach((name) => {
    const row = document.createElement('div');
    row.className = 'test-row';
    const label = document.createElement('span');
    label.className = 'test-name';
    label.title = `tests/${name}.py`;
    label.textContent = name;
    const button = document.createElement('button');
    button.className = 'ghost-btn test-run';
    button.textContent = 'Запустить';
    button.disabled = testsRunning;
    button.addEventListener('click', () => startTest(name));
    row.append(label, button);
    ui.testsList.append(row);
  });
}

function loadTests() {
  if (!ui.testsList) return Promise.resolve([]);
  if (!testsRunning) ui.testsList.textContent = 'Загружаю…';
  return request('list_tests')
    .then((reply) => {
      if (!reply || reply.ok === false) throw new Error(reply && reply.error ? reply.error : 'ошибка');
      testsKnown = Array.isArray(reply.tests) ? reply.tests.map(String) : [];
      renderTests(testsKnown);
      if (!testsRunning) setTestsState(`тестов: ${testsKnown.length}`);
      return testsKnown;
    })
    .catch((error) => {
      testsKnown = [];
      if (ui.testsList) ui.testsList.textContent = 'Нет связи с ядром.';
      setTestsState(`ошибка: ${error.message || error}`);
      return [];
    });
}

function startTest(name, fromQueue = false) {
  if (testsRunning) return;
  if (!testsKnown.includes(name)) {
    setTestsState(`неизвестный тест: ${name}`);
    return;
  }
  if (!fromQueue) testsQueue = [];
  currentTest = name;
  testOutputFor = null;
  setTestsBusy(true);
  beginTestOutput(name);
  setTestsState(`запуск ${name}…`);
  request('run_test', { name })
    .then((reply) => {
      if (reply && reply.ok === true && reply.started === true) {
        if (testOutputFor !== name) startTestUI(name);
        return;
      }
      const message = reply && reply.error ? reply.error : 'не удалось запустить';
      currentTest = null;
      testOutputFor = null;
      setTestCaption('');
      setTestsBusy(false);
      setTestsState(message);
      setTestBadge(false);
      appendTestOutput(`ERROR: ${message}`);
      if (testsQueue.length) setTimeout(startNextTest, 150);
    })
    .catch((error) => {
      currentTest = null;
      testOutputFor = null;
      setTestCaption('');
      setTestsBusy(false);
      setTestsState(`ошибка: ${error.message || error}`);
      setTestBadge(false);
      appendTestOutput(`ERROR: ${error.message || error}`);
      if (testsQueue.length) setTimeout(startNextTest, 150);
    });
}

function startNextTest() {
  if (testsRunning) return;
  const next = testsQueue.shift();
  if (!next) {
    setTestsState('очередь завершена');
    setTestsBusy(false);
    return;
  }
  startTest(next, true);
}

function runAllTests() {
  if (testsRunning) return;
  if (!testsKnown.length) {
    loadTests().then((names) => {
      if (!names.length) return;
      testsQueue = names.slice();
      startNextTest();
    });
    return;
  }
  testsQueue = testsKnown.slice();
  startNextTest();
}

function onTestDone(msg) {
  if (msg.name !== currentTest) return;
  const ok = Boolean(msg.ok);
  const code = msg.code !== undefined ? msg.code : '?';
  const duration = msg.duration_s !== undefined ? Number(msg.duration_s) : 0;
  setTestsBusy(false);
  setTestBadge(ok);
  setTestsState(`${msg.name}: ${ok ? 'PASS' : 'FAIL'} · ${duration.toFixed(2)} с`);
  appendTestOutput(
    `=== ${msg.name}: ${ok ? 'PASS' : 'FAIL'} (code ${code}, ${duration.toFixed(2)} с) ===`,
  );
  const hasQueue = testsQueue.length > 0;
  loadTests().then(() => {
    if (hasQueue) startNextTest();
  });
}

function initTests() {
  if (ui.testsRefresh) ui.testsRefresh.addEventListener('click', () => loadTests());
  if (ui.testsRunAll) ui.testsRunAll.addEventListener('click', runAllTests);
  setTestsBusy(false);
  setTestBadge(null);
  setTestCaption('');
}

/* --- пробелы --- */

function makeGapButton(label, handler) {
  const button = document.createElement('button');
  button.className = 'ghost-btn gap-action';
  button.textContent = label;
  button.addEventListener('click', handler);
  return button;
}

function renderGaps(gaps) {
  if (!ui.gapsBody) return;
  ui.gapsBody.replaceChildren();
  if (!gaps.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 5;
    cell.className = 'muted';
    cell.textContent = 'Пробелов нет.';
    row.append(cell);
    ui.gapsBody.append(row);
    return;
  }
  gaps.forEach((gap) => {
    const row = document.createElement('tr');
    const command = document.createElement('td');
    command.textContent = gap.command || '—';
    const intent = document.createElement('td');
    intent.textContent = gap.user_intent || '—';
    const attempts = document.createElement('td');
    attempts.textContent = String(gap.attempts_to_close ?? gap.attempts ?? 1);
    const closed = document.createElement('td');
    closed.textContent = gap.closed_how || (gap.closed_at ? 'да' : '—');
    const actions = document.createElement('td');
    actions.className = 'gap-actions';
    actions.append(
      makeGapButton('Закрыть вручную', () => {
        request('close_gap', { id: gap.id })
          .then((reply) => {
            setGapsState(reply.ok ? `пробел ${gap.id} закрыт` : 'не удалось закрыть');
            loadGaps();
          })
          .catch(() => setGapsState('нет связи с ядром'));
      }),
      makeGapButton('Попытаться закрыть', () => {
        setGapsState(`пробел ${gap.id}: пробую…`);
        request('attempt_gap', { id: gap.id }, 120000)
          .then((reply) => {
            setGapsState(reply.message || reply.error || 'готово');
            loadGaps();
          })
          .catch(() => setGapsState('не удалось: нет связи/таймаут'));
      }),
    );
    row.append(command, intent, attempts, closed, actions);
    ui.gapsBody.append(row);
  });
}

function setGapsState(text) {
  if (ui.gapsState) ui.gapsState.textContent = text;
}

function loadGaps() {
  if (!ui.gapsBody) return;
  setGapsState('Загружаю…');
  request('get_gaps', { limit: 50 })
    .then((reply) => {
      const gaps = Array.isArray(reply.gaps) ? reply.gaps : [];
      renderGaps(gaps);
      setGapsState(`пробелов: ${gaps.length}`);
    })
    .catch((error) => {
      ui.gapsBody.replaceChildren();
      setGapsState(`ошибка: ${error.message || error}`);
    });
}

function initGaps() {
  const refresh = $('#btn-gaps-refresh');
  if (refresh) refresh.addEventListener('click', loadGaps);
}

/* --- чат --- */

function setChatState(text) {
  if (ui.chatState) ui.chatState.textContent = text;
}

function setChatTyping(active) {
  if (ui.chatTyping) ui.chatTyping.hidden = !active;
  if (active) scrollChatToBottom();
}

function scrollChatToBottom() {
  if (!ui.chatFeed) return;
  ui.chatFeed.scrollTop = ui.chatFeed.scrollHeight;
}

function chatSourceLabel(source) {
  return source === 'voice' ? 'голос' : source === 'text' ? 'текст' : '';
}

function addChatTurn(turn, { autoScroll = true } = {}) {
  if (!ui.chatFeed || !turn || !turn.text) return;
  const role = turn.role === 'user' ? 'user' : turn.role === 'kivi' ? 'kivi' : '';
  if (!role) return;
  const empty = ui.chatFeed.querySelector('.chat-empty');
  if (empty) empty.remove();
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${role}`;
  const text = document.createElement('div');
  text.className = 'chat-bubble-text';
  text.textContent = String(turn.text);
  bubble.append(text);
  const meta = document.createElement('div');
  meta.className = 'chat-meta';
  if (role === 'kivi') {
    const route = document.createElement('span');
    route.className = 'chat-badge route';
    route.textContent = String(turn.route || 'chat');
    meta.append(route);
  }
  const source = document.createElement('span');
  source.className = 'chat-badge';
  source.textContent = chatSourceLabel(String(turn.source || ''));
  if (source.textContent) meta.append(source);
  if (meta.childElementCount) bubble.append(meta);
  ui.chatFeed.append(bubble);
  if (autoScroll) scrollChatToBottom();
}

function renderChatHistory(turns) {
  if (!ui.chatFeed) return;
  ui.chatFeed.replaceChildren();
  const items = Array.isArray(turns) ? turns : [];
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'chat-empty';
    empty.textContent = 'Пока пусто. Напиши или скажи Киви.';
    ui.chatFeed.append(empty);
    return;
  }
  items.forEach((turn) => addChatTurn(turn, { autoScroll: false }));
  scrollChatToBottom();
}

function loadChatHistory() {
  if (!ui.chatFeed) return;
  setChatState('Загружаю историю…');
  request('get_history', { limit: 50 })
    .then((reply) => {
      const turns = Array.isArray(reply.turns) ? reply.turns : [];
      renderChatHistory(turns);
      setChatState(`turn'ов: ${turns.length}`);
    })
    .catch((error) => {
      if (ui.chatFeed) {
        ui.chatFeed.textContent = 'Нет связи с ядром.';
      }
      setChatState(`ошибка: ${error.message || error}`);
    });
}

function sendChatMessage() {
  if (!ui.chatInput) return;
  const text = String(ui.chatInput.value || '').trim();
  if (!text) {
    setChatState('Пустое сообщение не отправляется');
    return;
  }
  ui.chatInput.value = '';
  setChatTyping(true);
  setChatState('Отправляю…');
  request('send_text', { text }, 180000)
    .then((reply) => {
      if (!reply.ok) {
        setChatTyping(false);
        setChatState(reply.error || 'не удалось отправить');
        return;
      }
      setChatState(`маршрут: ${reply.route || 'chat'}`);
      // Kivi-turn обычно уже пришёл по WS; страховка на случай потери события.
      setTimeout(() => {
        if (!ui.chatTyping || ui.chatTyping.hidden) return;
        setChatTyping(false);
        loadChatHistory();
      }, 300);
    })
    .catch((error) => {
      setChatTyping(false);
      setChatState(`ошибка: ${error.message || error}`);
    });
}

function initChat() {
  const refresh = $('#btn-chat-refresh');
  if (refresh) refresh.addEventListener('click', loadChatHistory);
  if (ui.chatForm) {
    ui.chatForm.addEventListener('submit', (event) => {
      event.preventDefault();
      sendChatMessage();
    });
  }
  if (ui.chatInput) {
    ui.chatInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendChatMessage();
      }
    });
  }
}


function initSchedule() {
  const add = ui.scheduleAdd;
  const save = $('#btn-schedule-save');
  if (add) {
    add.addEventListener('click', () => {
      if (ui.scheduleForm) ui.scheduleForm.hidden = !ui.scheduleForm.hidden;
    });
  }
  if (save) {
    save.addEventListener('click', () => {
      setMemoryState('Форма добавления события — заглушка; используй голосовую команду «запомни…».');
      if (ui.scheduleForm) ui.scheduleForm.hidden = true;
    });
  }
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
      if (page === 'chat') loadChatHistory();
      if (page === 'memory') loadMemory(memoryKind);
      if (page === 'skills') loadSkills();
      if (page === 'gaps') loadGaps();
      if (page === 'settings') loadTests();
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

function initScreenLook() {
  if (!ui.screenLook) return;
  ui.screenLook.addEventListener('click', () => {
    ui.screenLook.disabled = true;
    showToast('Смотрю экран…');
    request('send_text', { text: 'что на экране' }, 120000)
      .then((reply) => {
        if (!reply || reply.ok === false) {
          showToast(reply && reply.error ? reply.error : 'не удалось посмотреть экран');
          return;
        }
        showToast(reply.response || 'Экран обработан.');
        noteEvent(`экран: ${reply.response || 'готово'}`);
      })
      .catch((error) => showToast(`экран: ${error.message || error}`))
      .finally(() => {
        ui.screenLook.disabled = false;
      });
  });
}

function init() {
  initNav();
  initWindowControls();
  initVolume();
  initScreenLook();
  initMemory();
  initSkills();
  initGaps();
  initSchedule();
  initChat();
  initTests();
  setConnection(false);
  connect();
}

init();
