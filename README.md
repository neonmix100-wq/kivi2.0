# Kivi — состояние проекта

## Протокол работы
Владелец: Алексей (Лёша), не программист. Инженер даёт готовые промпты для
Cline + команды + «что прислать». Язык — русский. Тесты: mock по умолчанию,
--live для реальных вызовов; тесты пишут в отдельную БД, боевая data/kivi.db
не трогается тестами.

## Окружение
D:\Kivi, .venv, Python 3.12. RTX 4060 Ti 16GB, 32GB RAM. TZ Asia/Novosibirsk.
Windows user minea. Микрофон fifine SC3; выход наушники Temple Pro Purple
(принимает только 48 кГц). Устройства резолвить ПО ИМЕНИ (индексы дрейфуют).
ffmpeg/ffplay 9.0.2. OpenCode CLI 1.19.6 (V1), конфиг
C:\Users\minea\.config\opencode\opencode.json (model opencode/deepseek-v4.1-flash-free),
ключ в системном env OPENCODE_API_KEY (setx). Владелец путает CMD и PowerShell.
Node 24 / npm 11; Electron 45 установлен локально в app/ (node_modules в gitignore).

## Провайдеры
Polza https://polza.ai/api/v1, ключ .env LLM_API_KEY.
Тиры: cheap=deepseek/deepseek-v4.1-flash-1017, coder=google/gemini-4-hydrogen,
vision=openai/gpt-6.1-terra, strong=anthropic/claude-sonnet-5.5.
Эскалация: cheap 3 попытки -> coder 2 -> strong; intent без эскалации.
TTS: google/gemini-3.8-flash-tts, голос Leda — МЕНЯТЬ НЕЛЬЗЯ (решение владельца).
STT: локально faster-whisper large-v3-turbo, CUDA int8_float16, 0.2-0.5с; fallback Polza.
Код: OpenCode (0 руб). Поиск: duckduckgo-search. Эмбеддинги: baai/bge-m3.
Бюджет 3000 руб/мес.

## Запреты
Не читать и не логировать значения .env. Не просить админ-права.
Не ставить тяжёлые зависимости без необходимости. Не менять голос TTS.
Скиллам не давать os/subprocess/environ; .env и *.db закрыты брокером.

## Не регрессировать (починено)
- Звук через ffplay: один плеер + lock, уникальные temp-файлы.
- Whisper: читать wav через soundfile (не PyAV), передавать numpy.
- CUDA DLL: os.add_dll_directory из sysconfig purelib/nvidia/{cublas,cudnn,
  cuda_nvrtc}/bin ДО импорта faster_whisper.
- Галлюцинации: гейт RMS<0.002, длительность 0.4-15с,
  condition_on_previous_text=False, блок-лист фраз.
- Один экземпляр: сокет 127.0.0.1:47613 + чистое завершение + идемпотентный лог;
  scripts/kivi_stop.ps1, kivi_status.ps1.
- Маршрут ответа: planner route = skill|search|chat|action_gap; ровно ОДИН
  ответ на команду; «не умею» только для action_gap.
- Вводные фразы: библиотека законченных предложений (think/search/code),
  кэш data/tts_cache/fillers, кэп 12, прогрев строго фоновый, pick не блокирует.
- Поиск: reformulate_query(command, history) -> чистый запрос; retry при
  NOT_FOUND; summarize с историей; не искать по сырой реплике.
- Диалог: окно = тишина silence_s=8 после её ответа; конец по фразам
  «конец диалога/заверши диалог/отбой»; «спасибо» НЕ закрывает;
  пока говорит сама — входящие фразы отбрасываются.

## Карта модулей
kivi/audio/{capture,vad,recorder}; kivi/stt/{local,transcribe};
kivi/tts/{speak,cache,stream,fillers}; kivi/brain/{router,providers,budget,
session,persona}; kivi/memory/{vault,reflect,retrieve,inject};
kivi/skills/{registry,broker,loader,planner,llm_planner,gap_closer,search,reminders,recall};
kivi/wake/{detector,loop}; kivi/runtime; kivi/main;
kivi/api/{server,pending,client}; kivi/ui/overlay; app/ (Electron, vanilla).
Скиллы: skills/{time_date,file_search,assistant_volume,recall,remember,reminders,tasks_list};
skills/installed/ (gitignore).
Данные: data/kivi.db (usage, capability_gaps, messages), data/skills.db,
data/models, data/tts_cache, data/opencode_jobs, memory/ (Obsidian-vault),
data/logs/kivi.log.

## Тесты
smoke_{speak,audio,router,stt_local,wakeword,skills,planner,gap_closer,search,
opencode,flow,dialogue,search_context,tts_stream,memory,memory_deep,api,
volume_exit,reflect_sched,routine}; level_meter; cuda_check. Демо оверлея: tests/api_ask_demo.py.

## Текущий шаг (сдан, ждёт голосовой приёмки)
Recall отвечает по конкретному вопросу: перед чтением памяти cheap-LLM
(`task_type=intent`) извлекает `keywords/date_refs`; записи фильтруются по
подстроке keywords и по дате, пустой фильтр сохраняет прежний обзор.
Проверки: `python tests\smoke_routine.py`; регрессии
smoke_memory_deep, smoke_flow, smoke_dialogue, smoke_api.

## Roadmap
Вариант А (выбран): 1) оверлей-виджет — каркас Electron сдан, дальше наполнить
страницы Скиллы/Память/Пробелы и подключить реальные сценарии,
2) зрение (скриншоты, vision-тир).
Позже: установка скиллов с GitHub и генерация скиллов через OpenCode
(требуют оверлей), barge-in, хоткеи.
Непрерывный TTS + кроссфейд уже есть; первый звук 3-7с — потолок провайдера TTS.

## Известные особенности
OpenCode V1 не понимает V2-permissions — конфиг держать минимальным.
OpenCode запускать только в data/opencode_jobs/<uuid>/ (он умеет bash).
setx требует нового терминала. nanobind-leak soxr при выходе — косметика.
