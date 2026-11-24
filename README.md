# Kivi — голосовой ассистент

Kivi — локальный голосовой ассистент для Алексея (Лёши) на Windows. Он слушает
микрофон, распознаёт речь локальным Whisper, отвечает голосом, помнит договорённости,
ставит напоминания, ищет информацию в интернете, запускает OpenCode и показывает
состояние в Electron-приложении с оверлеем. Владелец — не программист: проект
должен оставаться запускаемым, проверяемым и понятным без отладки.

**Текущий статус:** голосовой тракт, wake-word «Киви», диалоговое окно, эхо-гейт,
скиллы, память, напоминания, поиск, OpenCode, Chat UI, тест-раннер и Windows-сборка
(NSIS installer + portable) уже работают. Зрение (скриншот экрана → vision-тир)
добавлено. Тесты — smoke; часть работает без
сети и платных вызовов, отдельные (`smoke_planner`, `smoke_search`,
`smoke_router --live`, `smoke_opencode --live`) используют реальные сервисы.

> Правила работы и безопасности — в `AGENTS.md` и `.clinerules`. Секреты хранятся
> только в `.env` или системном окружении; значения ключей в README не попадают.

## 1. Архитектура

Два независимых процесса:

```text
┌────────────────────────────── Windows ──────────────────────────────┐
│                                                                    │
│  Микрофон WASAPI 48 кГц                                            │
│      │                                                             │
│      ▼                                                             │
│  kivi.main (Python 3.12)                                           │
│  capture → VAD → recorder → STT (faster-whisper, 16 кГц)           │
│      → wake-детектор «Киви» → planner                              │
│          ├─ skill ──────► skills/*/handler.py (broker)             │
│          ├─ search ─────► DuckDuckGo api/html/lite + cache         │
│          ├─ code ───────► OpenCode CLI                             │
│          └─ chat ───────► Polza OpenAI-compatible LLM              │
│      → answer → TTS stream (Gemini TTS, Leda) → наушники 48 кГц    │
│                                                                    │
│  WebSocket API 127.0.0.1:8765  ◄──────────────►  Electron          │
│                                                    ├─ главное окно │
│                                                    └─ оверлей      │
└────────────────────────────────────────────────────────────────────┘
```

- **Ядро** — `python -m kivi.main`: захват, VAD, STT, wake, планировщик, скиллы,
  LLM, TTS, память, напоминания, API и одиночный экземпляр.
- **Приложение** — `app/` на Electron: главное окно с разделами «Обзор», «Чат»,
  «Логи», «Память», «Скиллы», «Пробелы», «Настройки» и прозрачный оверлей.
  В упакованном виде приложение запускает ядро из `D:\Kivi` само; если порт
  `8765` уже занят, второй экземпляр ядра не запускается.
- **Связь** — только loopback WebSocket `127.0.0.1:8765`; внешняя сеть для API
  не открывается.

## 2. Технологический стек

| Слой | Технология | Зачем |
|---|---|---|
| Захват звука | `sounddevice`, WASAPI shared, 48 кГц | Нативная частота микрофона без пересоздания потока на 16 кГц |
| VAD | Silero VAD через `onnxruntime`, CPU | Определение границ речи без GPU |
| STT | `faster-whisper` + CTranslate2, CUDA `int8_float16` | Локальное распознавание русского; CPU fallback |
| LLM | Polza, OpenAI-совместимый HTTP API | Тиры cheap/coder/vision/strong, стриминг ответа |
| TTS | Polza `google/gemini-3.8-flash-tts`, голос `Leda` | Голосовой ответ; base64 MP3 → PCM 48 кГц |
| Зрение | GDI `BitBlt` + Pillow → vision-тир | По запросу описать текущий экран |
| Память | Markdown-vault + SQLite + эмбеддинги `baai/bge-m3` | Факты, расписание, задачи, темы, люди и история диалога |
| Скиллы | Python-манифесты `skill.yaml` + брокер прав | Изолированные действия без `os`/`subprocess` у скилла |
| Поиск | `duckduckgo-search`, бэкенды api/html/lite | Живые данные без API-ключа |
| Код | OpenCode CLI | Задачи написания кода в отдельной папке |
| UI | Electron 45 + vanilla HTML/CSS/JS | Без сборщика и frontend-фреймворка |
| Упаковка | `electron-builder` | NSIS installer и portable exe |
| Эхо-гейт | CPU-корреляция playback reference | Не принимать собственный голос за команду |
| Устройства | `sounddevice` по имени + hostapi | Индексы WASAPI дрейфуют при переподключении |


## 3. Библиотеки и зависимости

### Python (`requirements.txt` + фактические версии из `pip freeze`)

Прямые зависимости проекта; версии — из текущего `.venv` (Python 3.12.10):

| Пакет | Версия | Назначение |
|---|---|---|
| `faster-whisper` | `1.2.1` | Локальное распознавание речи через CTranslate2 |
| `sounddevice` | `0.5.6` | WASAPI-потоки входа/выхода и выбор устройств |
| `soundfile` | `0.14.0` | Чтение/запись WAV для STT и тестов |
| `numpy` | `2.5.5` | Аудио-буферы, корреляция, обработка PCM |
| `onnxruntime` | `1.32.0` | Инференс Silero VAD на CPU |
| `requests` | `2.34.2` | HTTP к Polza/LLM/TTS и проверки |
| `loguru` | `0.8.0` | Логи приложения и ядра |
| `pydantic` | `2.14.3` | Валидация конфигов и манифестов |
| `pydantic-settings` | `2.16.3` | Настройки окружения |
| `PyYAML` | `6.0.3` | Чтение `config.yaml` и `skill.yaml` |
| `python-dotenv` | `1.2.4` | Загрузка `.env` без вывода секретов |
| `psutil` | `7.2.2` | Процессы/системная диагностика |
| `nvidia-ml-py` | `13.615.71` | Проверка NVIDIA GPU/VRAM |
| `soxr` | `1.1.0` | Ресемплинг 48 кГц → 16 кГц |
| `tzdata` | `2026.6` | Часовой пояс `Asia/Novosibirsk` на Windows |
| `nvidia-cublas-cu12` | `12.9.2.10` | CUDA cuBLAS DLL для faster-whisper |
| `nvidia-cudnn-cu12` | `9.28.1.4` | CUDA cuDNN DLL для faster-whisper |
| `duckduckgo-search` | `8.1.1` | Поиск DuckDuckGo без API-ключа |
| `websockets` | `17.4` | WebSocket API `127.0.0.1:8765` и клиенты |
| `Pillow` | `13.0.0` | Скриншот → PNG для vision-тира |

`torch` в окружении не установлен; STT работает на faster-whisper/CTranslate2,
VAD — на ONNX Runtime. CUDA-библиотеки подключаются через `nvidia-*` pip-пакеты,
а не через отдельную установку CUDA Toolkit.

### Node (`app/package.json`)

| Пакет | Версия в манифесте | Фактически в lock | Назначение |
|---|---|---|---|
| `electron` | `^45.4.1` | `45.4.1` | Desktop-оболочка UI |
| `electron-builder` | `^26.18.0` | `26.18.0` | NSIS installer и portable exe |

Production-зависимостей нет; webpack/Vite/React и другие сборщики не используются.
Запуск UI: `npm start`. Сборка: `npm run dist` или `npm run pack`.

### Системные prerequisites

| Компонент | Требование | Как ставится |
|---|---|---|
| ОС | Windows 10/11 x64 | вручную |
| Python | 3.12.x, виртуальное окружение `.venv` | вручную; зависимости — `pip install -r requirements.txt` |
| Node.js/npm | Node 24.x, npm 11.x (проверено: 24.21.0 / 11.19.0) | вручную |
| ffmpeg/ffplay | 9.0.2 (gyan.dev) | вручную, обычно `winget install Gyan.FFmpeg` |
| CUDA DLL | `nvidia-cublas-cu12`, `nvidia-cudnn-cu12` | автоматически через `pip` |
| OpenCode CLI | 1.19.6, модель `opencode/deepseek-v4.1-flash-free` | `npm install -g opencode-ai`; конфиг вне репозитория |
| Аудио | WASAPI, вход «Микрофон (fifine SC3)», выход «Наушники (2- Temple Pro Purple)» | выбрать в Windows; имена уже в `config.yaml` |
| GPU | RTX 4060 Ti 16 ГБ, драйвер NVIDIA | вручную; VRAM контролируется конфигом и бюджетом |


## 4. Модели и внешние сервисы

| Назначение | Провайдер / модель | Где ключ или конфиг |
|---|---|---|
| LLM cheap (chat/intent/memory) | Polza `deepseek/deepseek-v4.1-flash-1017` | `LLM_BASE_URL`, `LLM_API_KEY`; `brain.tiers.cheap` |
| LLM coder | Polza `google/gemini-4-hydrogen` | `brain.tiers.coder` |
| LLM vision | Polza `openai/gpt-6.1-terra` | `brain.tiers.vision` |
| LLM strong | Polza `anthropic/claude-sonnet-5.5` | `brain.tiers.strong` |
| Оркестратор | `deepseek/deepseek-v4.1-flash-1017` | `brain.orchestrator` |
| Локальный профиль (резерв) | `qwen3-14b-instruct-q4_k_m`, ctx 8192 | `brain.local` |
| TTS | Polza `google/gemini-3.8-flash-tts`, voice `Leda` | `TTS_MODEL`, `TTS_VOICE`; `tts.*`; голос менять нельзя |
| STT | локальный `faster-whisper large-v3-turbo` | `stt.primary=local`, CUDA `int8_float16`; fallback Polza `stt.fallback=polza` |
| Эмбеддинги памяти | `baai/bge-m3` | `memory.embed_model`; OpenAI-совместимый endpoint |
| Код | OpenCode CLI, `opencode/deepseek-v4.1-flash-free` | внешний `opencode.json`; не в README |
| Поиск | DuckDuckGo `api` → `html` → `lite` | `duckduckgo-search`; API-ключ не нужен |
| Напоминания | локальный JSON + фоновый поток | `data/reminders.json` |

**Эскалация:** cheap до 3 попыток → coder до 2 → strong; `intent` без эскалации.
TTS возвращает JSON с base64-аудио, а не сырой MP3; клиент декодирует base64,
ресемплит в 48 кГц и играет в наушники.

**Бюджет:** `brain.budget.monthly_rub: 3000` руб/мес (дублируется в
`budget.brain_monthly_rub`), голосовой лимит `budget.voice_monthly_rub: 800`,
предупреждение при `warn_at: 0.8`. Расходы пишутся в `data/kivi.db` (таблица
`usage`), проверка лимита выполняется до запроса. `on_exceed:
fallback_local_and_warn`.

**Секреты:** только `.env`/системное окружение. Используются имена
`LLM_BASE_URL`, `LLM_API_KEY`, `TTS_MODEL`, `TTS_VOICE`, `OPENCODE_API_KEY`.
Значения в README, логах, git и скриншотах не публикуются.

## 5. Карта модулей

### Ядро `kivi/`

| Модуль | Назначение |
|---|---|
| `kivi/main.py` | Точка входа: single-instance, WakeLoop, scheduler напоминаний, прогрев STT/TTS, привязка API |
| `kivi/config.py` | `config.yaml` → pydantic, разрешение WASAPI-устройств по имени |
| `kivi/conversation.py` | Старый одиночный цикл «микрофон → VAD → STT → LLM → TTS» |
| `kivi/prefs.py` | `data/prefs.json`: громкость TTS |
| `kivi/log.py` | loguru: DEBUG в `data/logs/kivi.log`, INFO в консоль |
| `kivi/runtime.py` | TCP-lock `127.0.0.1:47613`, signals, cleanup |
| `kivi/audio/capture.py` | WASAPI 48 кГц, кольцевой буфер, ресемплинг soxr |
| `kivi/audio/vad.py` | Silero VAD на CPU, границы речи |
| `kivi/audio/recorder.py` | Сбор utterance и pre-roll |
| `kivi/audio/output.py` | Выбор выхода по имени/default, playback lock |
| `kivi/audio/echo.py` | Эхо-гейт по корреляции с воспроизведением |
| `kivi/audio/pipeline.py` | Поток «захват → VAD → Utterance» |
| `kivi/stt/local.py` | faster-whisper CUDA + CUDA DLL bootstrap, CPU fallback |
| `kivi/stt/transcribe.py` | Резервное распознавание через OpenAI-совместимый endpoint |
| `kivi/tts/speak.py` | TTS → MP3 → PCM 48 кГц → наушники |
| `kivi/tts/cache.py` | Кэш фиксированных TTS-фраз `data/tts_cache` |
| `kivi/tts/stream.py` | Непрерывный OutputStream, чанки и кроссфейд |
| `kivi/tts/fillers.py` | Библиотека вводных фраз и её прогрев |
| `kivi/brain/router.py` | Маршрутизация задач, тиры, эскалация, стриминг |
| `kivi/brain/providers.py` | Polza/OpenAI-совместимый HTTP-провайдер |
| `kivi/brain/budget.py` | Учёт рублей в SQLite, лимит до запроса |
| `kivi/brain/session.py` | История SQLite `messages`/`sessions`, `route`/`source` |
| `kivi/brain/persona.py` | Персона Киви и подготовка текста к речи |
| `kivi/memory/vault.py` | Markdown-vault: profile/schedule/tasks/topics/people |
| `kivi/memory/reflect.py` | Фоновая рефлексия и её планировщик |
| `kivi/memory/retrieve.py` | Поиск фактов по смыслу через эмбеддинги |
| `kivi/memory/inject.py` | Блок памяти в промпт |
| `kivi/skills/registry.py` | Сканирование `skill.yaml`, enabled-флаги, статистика |
| `kivi/skills/loader.py` | Загрузка и вызов handler с таймаутом |
| `kivi/skills/broker.py` | Единственный API скилла: сеть/файлы/tools по permissions |
| `kivi/skills/planner.py` | LLM-планировщик + difflib fallback, route |
| `kivi/skills/llm_planner.py` | JSON-ответ планировщика и каталог умений |
| `kivi/skills/gap_closer.py` | Попытка закрыть пробел в умениях |
| `kivi/skills/gaps.py` | Таблица `capability_gaps` в `data/kivi.db` |
| `kivi/skills/search.py` | DDGS-бэкенды, кэш, reformulate, summarize |
| `kivi/skills/reminders.py` | `data/reminders.json` и фоновый scheduler |
| `kivi/skills/recall.py` | Targeted recall расписания/задач из vault |
| `kivi/wake/detector.py` | Поиск имени «Киви», fuzzy по Левенштейну |
| `kivi/wake/loop.py` | VAD/wake/dialog/пауза, turn-lock, turn-события |
| `kivi/api/server.py` | WebSocket API, статус, команды, тест-раннер |
| `kivi/api/pending.py` | ask/confirm с Event и таймаутом |
| `kivi/api/client.py` | Синхронный WS-клиент для тестов/утилит |
| `kivi/ui/overlay.py` | OverlayService: ask/confirm/info через API |
| `kivi/vision/screen.py` | GDI BitBlt-захват, downscale PNG, debug `save_last` |
| `kivi/lipsync/`, `kivi/safety/`, `kivi/speaker/` | Зарезервированные пакеты/точки расширения |


### Скиллы `skills/`

| Скилл | Категория | Права из `skill.yaml` | Что делает |
|---|---|---|---|
| `time_date` | system | tools: —, risk: safe | Время и дата по Новосибирску |
| `file_search` | files | fs: `D:\Kivi\data`, `D:\Kivi\skills`; tool `fs_list`; safe | Поиск файлов по имени |
| `assistant_volume` | system | tool `self_volume`; safe | Своя громкость Киви |
| `recall` | data | fs: `D:\Kivi\memory`; tools `fs_read`, `reminders_read`, `llm_intent`; safe | Расписание и открытые задачи |
| `remember` | data | fs: `D:\Kivi\memory`; tool `self_memory`; safe | Факт/событие/задача/предпочтение |
| `reminders` | system | fs: `D:\Kivi\memory`; tools `scheduler`, `self_memory`; safe | Напоминание ко времени |
| `tasks_list` | data | fs: `D:\Kivi\memory`; tools `fs_read`, `reminders_read`; safe | Список открытых задач |
| `listen_control` | system | tool `listen`; safe | Пауза/возобновление VAD |
| `memory_manage` | data | fs: `D:\Kivi\memory`; tools `fs_read`, `fs_write`, `reminders_write`, `confirm`; risk: confirm | Очистка и закрытие записей только после подтверждения |
| `capabilities` | system | tool `skills_read`; safe | Честный список включённых умений |
| `screen_look` | vision | tools `screenshot`, `vision`; safe | Скриншот экрана и короткое описание |

`skills/installed/` — каталог устанавливаемых скиллов, в git не входит.

## 6. Данные и пути

| Путь | Что там |
|---|---|
| `data/kivi.db` | SQLite: `usage`, `sessions`, `messages`, `capability_gaps` |
| `data/skills.db` | Статистика скиллов и ручные enabled-флаги, `skill_flags` |
| `data/models/` | Локальная модель faster-whisper `large-v3-turbo` |
| `data/tts_cache/` | Кэш TTS mp3; `fillers/` — кэш вводных фраз |
| `data/opencode_jobs/` | Рабочие папки OpenCode, по одной на задачу (`<uuid8>`) |
| `data/reminders.json` | Напоминания: `id`, ISO-время с датой, текст, `done` |
| `data/search_cache.json` | Кэш DDGS: нормализованный запрос → `ts`, `results`, TTL 600 с |
| `data/prefs.json` | Пользовательская громкость TTS (вне git) |
| `data/tmp/last_screen.png` | Последний скриншот только при `vision.save_last: true` |
| `memory/` | Markdown-vault: `profile.md`, `schedule.md`, `tasks.md`, `topics/`, `people/`, `journal/` |
| `data/logs/kivi.log` | DEBUG/INFO лог ядра с ротацией 10 МБ |
| `data/logs/wake.log` | Распознанные фразы и решения wake-цикла |
| `%APPDATA%\kivi-desktop\kivi-app.log` | Лог main-процесса Electron в packaged-режиме |
| `app/dist/` | Собранные `Kivi Setup *.exe`, portable `Kivi *.exe`, `win-unpacked/` |
| `data/tmp/` | Временные файлы и смоук-БД |


## 7. Конфигурация

Основной файл — `config.yaml`; значения ниже являются дефолтами/фактическим
содержимым текущего файла.

| Блок | Ключевые поля и дефолты | Зачем |
|---|---|---|
| `audio` | `hostapi: Windows WASAPI`; input `Микрофон (fifine SC3)`; output `Наушники (2- Temple Pro Purple)`; capture 48000; target 16000; playback 48000; `soxr`; mono; block 32 мс; `aec: false` | Звук, устройства и ресемплинг |
| `audio.echo` | `enabled: true`, `corr_threshold: 0.6`, `tail_ms: 600`, `max_delay_ms: 400` | Эхо-гейт |
| `stt` | `primary: local`, `fallback: polza`, `model: large-v3-turbo`, `compute_type: int8_float16`, `language: ru`, `beam_size: 1` | Распознавание |
| `brain` | orchestrator/tiers cheap/coder/vision/strong, routing, escalation 3→2→strong, `budget.monthly_rub: 3000`, `warn_at: 0.8`, `timezone: Asia/Novosibirsk` | LLM-маршруты и деньги |
| `brain.local` | `model: qwen3-14b-instruct-q4_k_m`, `ctx: 8192`, `gpu_layers: all` | Резервный локальный профиль |
| `tts` | `primary: polza_gemini`, `google/gemini-3.8-flash-tts`, `voice: Leda`, `streaming: true`, `base64_response: true`, `audio_format: mp3` | Голос; голос менять нельзя |
| `skills` | `dir: skills`, `installed_dir: skills/installed`, auto-disable 3, timeout 30 с; browser/vision/music/telegram/home/rag | Реестр и политика скиллов |
| `planner` | `strategy: llm_with_fallback`, `catalog_max_tokens: 600`, `fallback_on_parse_error: true` | Планировщик умений |
| `gap_closer` | `min_frequency: 3`, `reactivate_threshold: 0.7`, `compose_enabled: true`, `ask_enabled: true` | Закрытие пробелов |
| `search` | `enabled: true`, `max_results: 3`, `timeout_s: 15`, `fact_keywords` (кто/что/когда/…) | DuckDuckGo |
| `conversation` | `enabled: true`, `silence_s: 8`, `end_phrases` (конец диалога/заверши диалог/отбой), `greeting_phrases` | Окно диалога |
| `memory` | `enabled: true`, `vault_path: memory`, kinds profile/schedule/tasks/topics/people, reflect 20 реплик/10 мин/60 с/300 с, `retrieve_k: 4`, `embed_model: baai/bge-m3` | Долгая память |
| `api` | `host: 127.0.0.1`, `port: 8765`, `ask_timeout_s: 60`, `confirm_timeout_s: 120`, `text_speaks: true` | WebSocket UI |
| `budget` | `brain_monthly_rub: 3000`, `voice_monthly_rub: 800`, `on_exceed: fallback_local_and_warn` | Общий лимит |
| `vision` | `max_side: 1280`, `save_last: false` | Скриншот и отладочный файл |
| `persona`/`user` | имя Киви, wake_names `[киви]`, стиль, `address_as: Лёша`; `user.timezone` пока `УТОЧНИТЬ` | Персона и обращение |
| `speaker` | `enabled: true`, `gate: tools_only`, `threshold: 0.65` | Гейт исполнителя (по ТЗ) |
| `safety` | confirm: delete/overwrite_outside_workspace/terminal_outside_workspace/install/send_email/purchase/password; sandbox `D:\Kivi\workspace`; trash_only; git_checkpoint | Безопасность |
| `overlay` | Отдельного блока в `config.yaml` нет; геометрия окон задана в `app/main.js` (`PILL_SIZE 260×48`, `CARD_MAX 460×420`, `OVERLAY_MARGIN 16`) | Оверлей |

## 8. Запуск

### Ядро отдельно

```powershell
cd D:\Kivi
.\.venv\Scripts\python.exe -m kivi.main
```

Приложение само запускает это ядро из `D:\Kivi`, если порт `8765` свободен.
`python -m kivi.main` без API: `python -m kivi.main --no-api`.

### Приложение

```powershell
cd D:\Kivi\app
npm install
npm start
```

Установленное приложение запускается ярлыком **Kivi** на рабочем столе.
`kiviHome` ищется в порядке:
`KIVI_HOME` → `%APPDATA%\kivi-desktop\kivi-home.json` → `D:\Kivi`.

### Упаковка

```powershell
cd D:\Kivi\app
npm run dist
npm run pack
```

`npm run dist` создаёт `app/dist/Kivi Setup 0.1.0.exe` и
`app/dist/Kivi 0.1.0.exe` (portable). `npm run pack` — только `win-unpacked`.

### Остановка и статус

```powershell
powershell -ExecutionPolicy Bypass -File scripts\kivi_stop.ps1
powershell -ExecutionPolicy Bypass -File scripts\kivi_status.ps1
```

`kivi_stop.ps1` завершает только `python -m kivi.main` и `ffplay`; `kivi_status.ps1`
показывает PID, uptime и занятость порта-замка `47613`.

### Тесты

```powershell
python tests\smoke_<name>.py
python tests\smoke_vision.py
python tests\smoke_opencode.py --live
python tests\smoke_router.py --live
```

`tests\run_all.bat` в текущем дереве **отсутствует**; запускай выбранные smoke
по одному или через UI: Настройки → Тесты → нужный тест / «Прогнать все».
`smoke_planner` и `smoke_search` обращаются к реальным LLM/поиску; `--live`
включает реальный вызов в `smoke_router` и `smoke_opencode`. Тест-раннер работает
только с whitelist `tests/smoke_*.py`, запускает один тест одновременно и имеет
timeout 300 с.


## 9. Протокол API (WebSocket)

Сервер: `ws://127.0.0.1:8765`, только loopback. Ответ на команду — JSON с `ok`.
События — JSON с полем `event`.

### События core → клиент

| Событие | Поля | Когда |
|---|---|---|
| `status` | `state`, `budget_used`, `volume`, `window_open` | При подключении и смене состояния/громкости |
| `turn` | `role` (`user`/`kivi`), `text`, `ts`, `route`, `source` | Голосовой или текстовый turn |
| `ask` | `id`, `question`, `options` | Нужен ответ пользователя |
| `confirm` | `id`, `title`, `body`, `details` | Нужно подтверждение действия |
| `info` | `text` | Короткое сообщение UI |
| `log` | `level`, `line` | Поток INFO+ из кольца логов |
| `test_line` | `name`, `line` | Строка вывода запущенного smoke-теста |
| `test_done` | `name`, `ok`, `code`, `duration_s` | Тест завершён; `ok` здесь часть события, не ответ команды |

### Команды клиент → core

| Команда | Поля | Назначение |
|---|---|---|
| `send_text` | `text` | Текст в тот же `handle_command`, что и голос |
| `get_history` | `limit` | Хронологические turn'ы текущей сессии |
| `set_volume` | `v` (0..1) | Своя громкость TTS |
| `get_gaps` | `limit` | Список пробелов в умениях |
| `get_memory` | `kind` | Строки/элементы памяти |
| `reflect_now` | — | Принудительная рефлексия |
| `reminders_list` | — | Напоминания, включая done |
| `cancel_reminder` | `id` | Отменить напоминание |
| `list_tests` | — | Имена `tests/smoke_*.py` без пути/расширения |
| `run_test` | `name` | Запустить только тест из list_tests; иначе ошибка/busy |
| `ask_response` | `id`, `answer` | Ответ на `ask` |
| `confirm_response` | `id`, `ok` | Ответ на `confirm` |
| `shutdown` | — | Корректно остановить ядро |
| `get_status` | — | Снимок статуса |
| `get_skills` | — | Список скиллов и статистики |
| `set_skill_enabled` | `name`, `enabled` | Включить/выключить скилл |
| `test_skill` | `name` | Проверка скилла (заглушка) |
| `close_gap` | `id` | Закрыть пробел вручную |
| `attempt_gap` | `id` | Попытаться закрыть пробел |
| `tail_logs` | `n` | Последние строки кольцевого лога |
| `ask` / `confirm` / `info` | как у событий | Серверные запросы/сообщения через pending |

`run_test` не принимает произвольные пути: допустимы только имена из
`list_tests`; одновременно один тест, второй запуск возвращает
`{"ok": false, "error": "busy"}`.


## 10. Что уже работает (чек-лист)

- [x] **Голосовой тракт:** микрофон WASAPI 48 кГц → VAD → запись utterance →
  faster-whisper → ответ → TTS → наушники 48 кГц.
- [x] **Wake «Киви» и диалог:** имя, fuzzy-варианты, окно тишины `silence_s=8`,
  фразы завершения; пауза/возобновление прослушки (`loop.paused`).
- [x] **Эхо-гейт:** корреляция с playback reference; динамический выбор выхода
  по имени/default; один playback lock и уникальные temp-файлы.
- [x] **Скиллы:** `time_date`, `file_search`, `assistant_volume`, `recall`,
  `remember`, `reminders`, `tasks_list`, `listen_control`, `memory_manage`,
  `capabilities`.
- [x] **Память и рутина:** markdown-vault, SQLite-сессии, рефлексия, targeted recall,
  напоминания с датой, confirm перед очисткой.
- [x] **Поиск:** DDGS api/html/lite, reformulate, summary, кэш 600 с и отдельный
  ответ при отказе всех бэкендов.
- [x] **OpenCode-код:** отдельная рабочая папка на задачу, mock/`--live` тесты.
- [x] **Зрение:** `screen_look` делает скриншот только по команде, сжимает PNG
  до `vision.max_side` и просит vision-тир описать экран.
- [x] **Chat UI:** история, голосовые и текстовые turn'ы, общий `handle_command`,
  `send_text`, `turn`-события.
- [x] **Оверлей и главное окно:** Electron, статус, громкость, логи, память,
  скиллы, пробелы, чат, настройки и оверлей поверх рабочего стола.
- [x] **Тест-раннер:** список smoke-тестов, запуск из «Настроек», поток строк,
  PASS/FAIL, «Прогнать все», whitelist и один тест одновременно.
- [x] **Windows-упаковка:** `npm run dist` → NSIS installer и portable exe;
  установленное приложение запускает ядро из `D:\Kivi` и останавливает его при
  выходе; в packaged-режиме devtools закрыты.

## 11. Не регрессировать (починенные баги)

- Звук через `sounddevice/output.py`: выбор выхода по имени/default, AEC по
  `PlaybackRef`; один playback lock и уникальные temp-файлы TTS.
- Whisper: читать WAV через `soundfile` (не PyAV), передавать numpy.
- CUDA DLL: `os.add_dll_directory` из `sysconfig purelib/nvidia/{cublas,cudnn,cuda_nvrtc}/bin`
  **до** импорта `faster_whisper`.
- Галлюцинации STT: гейт RMS `< 0.002`, длительность 0.4–15 с,
  `condition_on_previous_text=False`, блок-лист фраз.
- Один экземпляр: сокет `127.0.0.1:47613` + чистое завершение + идемпотентный лог;
  `scripts/kivi_stop.ps1`, `kivi_status.ps1`.
- Маршрут ответа: planner `route=skill|search|chat|action_gap`; ровно **один**
  ответ на команду; «не умею» только для `action_gap`.
- Вводные фразы: библиотека законченных предложений (think/search/code), кэш
  `data/tts_cache/fillers`, кэп 12, прогрев строго фоновый, `pick` не блокирует.
- Поиск: `reformulate_query(command, history)` → чистый запрос; retry при
  `NOT_FOUND`; summary с историей; по сырой реплике не искать.
- Диалог: окно = тишина `silence_s=8` после её ответа; конец по фразам
  «конец диалога/заверши диалог/отбой»; «спасибо» **не** закрывает; пока говорит
  сама — входящие фразы отбрасываются.
- Пауза прослушки: wake-детекция работает, VAD-фразы игнорируются, кроме
  resume; текст/API доступны всегда; состояние — `loop.paused`.
- Поиск: DDGS `api → html → lite`, кэш `data/search_cache.json` TTL 600 с;
  все бэкенды упали → «Поиск временно недоступен, попробую чуть позже.».
- Эхо и динамический вывод: смена default-устройства видна без перезапуска,
  собственный голос не становится командой.
- Рефлексия: scheduler не крутит busy-loop; не чаще `reflect_min_interval_s=300`.
- Targeted recall: `во сколько мне…` / `что завтра` получает исходную команду,
  а не общий список памяти.
- Напоминания: дата/время парсятся с днём; прошедшее время переносится корректно.
- `memory_manage`: перед очисткой/удалением обязательный `confirm`; без UI/отмены
  ничего не удаляется.
- Тест-раннер: только whitelist `tests/smoke_*.py`; путь/имя вне списка
  отклоняются; одновременно один тест, timeout 300 с.


## 12. План / TODO

Старый roadmap сохранён:

Вариант А (выбран): 1) оверлей-виджет — каркас Electron сдан, дальше наполнить
страницы Скиллы/Память/Пробелы и подключить реальные сценарии,
2) зрение (скриншоты, vision-тир).
Позже: установка скиллов с GitHub и генерация скиллов через OpenCode
(требуют оверлей), barge-in, хоткеи.
Непрерывный TTS + кроссфейд уже есть; первый звук 3-7с — потолок провайдера TTS.

Дополнительно:

- [x] UI тесты: при запуске нового теста очищать консоль и показывать вывод только последнего запущенного теста.
- [x] Упаковка в exe (electron-builder) — сделано: NSIS installer `Kivi Setup 0.1.0.exe`
  и portable `Kivi 0.1.0.exe`.
- [ ] Содержательные навыки: почта, файлы, умный дом (по одному через конвейер).
- [ ] Верификация голоса (опционально, слой 3 эхо/шум).
- [ ] Barge-in и хоткеи.

## Известные особенности

- OpenCode V1 не понимает V2-permissions — конфиг держать минимальным.
- OpenCode запускать только в `data/opencode_jobs/<uuid>/` (он умеет bash).
- `setx` требует нового терминала.
- nanobind-leak soxr при выходе — косметика.
- `tests/run_all.bat` отсутствует; используй отдельные smoke или UI-раннер.
- `user.timezone` в `config.yaml` помечен `УТОЧНИТЬ`; код бюджета/напоминаний
  использует `Asia/Novosibirsk`.
- В packaged-режиме `app.getPath("userData")` — `%APPDATA%\kivi-desktop`;
  `kivi-home.json` и `kivi-app.log` лежат там.
