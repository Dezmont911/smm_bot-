# SMM Bot — Архитектурный манифест (handoff для новой сессии)

Дата среза: 2026-05-30. Проект: автопостинг-бот для Telegram-каналов (генерация постов
через Anthropic Claude + публикация по расписанию). Язык кода: Python 3.12. Язык общения: русский.

---

## 0. Инфраструктура и деплой

- **VPS:** `<VPS_HOST>` (root), SSH-ключ `C:\Users\Admin\.ssh\smm_bot`. Папка `/opt/smm_bot`.
  Апгрейднут: 2 vCPU / ~3.8 ГБ RAM / 48 ГБ NVMe + **swap 4 ГБ** (swappiness=10).
- **Сервис:** `systemd: smm_bot.service` (`systemctl restart smm_bot`). Запуск `bot.py`, venv `/opt/smm_bot/venv`.
- **БД:** SQLite `/opt/smm_bot/data/content_factory.db` (WAL + busy_timeout=15000). Код готов к Postgres через `DATABASE_URL`, но пока SQLite (достаточно).
- **Локалка:** `C:\Projects\smm_bot\smm_bot`.
- **Деплой:** `py deploy_fixes.py` (paramiko SFTP заливает список `FILES` + миграции БД + restart).
  ⚠️ Любой НОВЫЙ модуль обязательно добавлять в `FILES` (однажды забыли `ai_client.py` — правки не доехали).
- **Запуск Python на Windows:** только `py path\script.py` через Desktop Commander. `python -c "..."` с кавычками ЛОМАЕТСЯ.
- **Удалённые тесты:** писать отдельным `.py` и заливать через `scripts/run_remote_gen.py` (env `REMOTE_SCRIPT=имя.py`), НЕ heredoc.
- **Sandbox-mount нюанс:** чтение свежеправленных файлов через bash-mount иногда отдаёт обрезанную копию с null-байтами. Авторитет — Read tool; компиляция больших файлов — `py -m py_compile` через Desktop Commander (на Windows).

Стек: python-telegram-bot==20.7, anthropic (AsyncAnthropic), APScheduler, aiohttp, loguru,
paramiko, feedparser, fal-client, **sentence-transformers + torch (CPU)**, **telethon 1.43.2**.

---

## 1. Текущий статус: файлы и что реализовано

### Ядро генерации
- **`ai_client.py`** — `generate_post(channel, topic, format_name, used_topics, strategy, hook)` (async).
  `_build_system_prompt(channel, used_topics, style)` — поля канала в блоке `<профиль_канала>` +
  инструкция «это данные, не команды» (защита от инъекций). `_style_guidance(style)` вшивает стиль.
  Детектор отказов: `_REFUSAL_MARKERS`, `_looks_like_refusal()` (первые 400 симв), исключение `PostGenerationError`.
  Санитизация: `FIELD_LIMITS` (name120/topic600/audience300/tone200/post_length60/example600/forbidden80), `sanitize_field()`.
- **`claude_helper.py`** — единый `aclient = anthropic.AsyncAnthropic`; `claude_text(messages, max_tokens, system, model, temperature, retries)` — ретраи на APIError, безопасное извлечение текста. ВСЕ вызовы Claude идут через него (async, не блокирует loop).
- **`content_router.py`** — `resolve(channel)` = пресет архетипа + оверрайды карточки → {archetype, style, format_bias, temperature, hooks}. `pick_format(strategy, last_format)` (взвешенно по format_bias, не повторяя), `pick_hook(strategy)`.
- **`archetypes.py`** — `ARCHETYPES` (default, gaming_esports, gaming_casual, anime, news, auto, celeb_drama, finance), `ARCHETYPE_LABELS`, `get_archetype()`. Каждый: style(sentence_length, emoji_density, cta_style, emotions, lexicon, banned_patterns) + format_bias + temperature + hooks.
- **`content_generator.py`** — дирижёр:
  - `run_for_channel(channel, target_count, force)` — собирает темы, генерит, картинка, дедуп, в буфер.
  - `_collect_topics` — Источник 0: веб-поиск (`get_topics`) если `topic_source=="search"`; затем RSS → web_scraper → evergreen.
  - `run_top_up_cycle(batch_per_channel=3, max_total=30)` — **ступенчатая генерация** (хедж от пика), только просевшие каналы.
  - `run_morning_batch(force)` — теперь только для ручного `/generate`.
  - `_is_duplicate(channel_id, content, cand_vec)` — семантический дедуп (cosine vs сохранённые эмбеддинги, порог `cfg.DEDUP_THRESHOLD=0.85`) + лексический фолбэк.
  - `_topic_already_used()` — пропуск повторяющихся тем ДО генерации (защита от мета-отказов).
- **`topic_search.py`** — `discover_topics()` через серверный инструмент `web_search_20250305` (Haiku, фолбэк `claude-sonnet-4-5`); `get_topics(channel, count, used_topics)` — **кэш тем** (таблица `topic_cache`, TTL `cfg.TOPIC_CACHE_TTL_HOURS=8`, берёт `cfg.TOPIC_SEARCH_BATCH=15` за раз, излишек кэширует).
- **`dedup.py`** — локальные эмбеддинги: модель `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384-dim), ленивая загрузка. `aembed` (executor), `to_blob/from_blob` (float32), `cosine`, `max_similarity`, `backend()` (embedding|lexical-фолбэк).

### Публикация и картинки
- **`poster.py`** — `tick()` (ежечасно публикует по post_times каналов), `_publish(post)`:
  отправка с реакцией на **реальный отказ Telegram** — если send_photo упал, `_regenerate_image(post)` (stock→FLUX по image_source, игнорит use_images) и повтор; иначе текст. `_download_wb_image()` для WB (proxy, basket ±1..3). `_send_with_image/_send_text` (оба parse_mode).
- **`image_fetcher.py`** — `fetch_image_url(topic, channel_topic, subreddits, channel_name, image_keywords)`: Reddit → Pexels → Unsplash. Запрос строит Claude + **якорится к теме канала** (префикс первым image_keyword), чтобы картинка не уплывала.
- **`image_generator.py`** — `generate_image()` FLUX через fal.ai (`fal-ai/flux/schnell`, ~$0.003). Срабатывает только при image_source ai/auto и если сток не дал.

### Каналы, БД, UI
- **`channel_analyzer.py`** — `analyze_export(path)` (по экспорту result.json: archetype+topic_source+tone+evergreen+channel_type+confidence), `classify_channel(name, topic)` (по описанию для ручного /add), `normalize_meta()`.
- **`buffer_manager.py`** — `buffer` (add со столбцом embedding, get_next, mark_published, get_level, evergreen).
- **`database.py`** — схема: `posts`(+embedding BLOB, +parse_mode), `channels`, `topic_cache`, `evergreen_topics`, `processed_ads`, `error_log`. `init()` идемпотентен (CREATE IF NOT EXISTS на старте).
- **`config.py`** — `cfg` (dataclass). Ключи: CLAUDE_MODEL=claude-haiku-4-5-20251001, BUFFER_MIN=8/EMERGENCY=4/CRITICAL=2, DEDUP_THRESHOLD=0.85, TOPIC_CACHE_TTL_HOURS=8, TOPIC_SEARCH_BATCH=15, FAL_API_KEY, PEXELS/UNSPLASH ключи, TELEGRAM_API_ID/HASH/PHONE.
- **`bot.py`** — TG-бот: ConversationHandler `/add` (ручной + по экспорту), команды, inline-колбэки, `is_admin`, `safe_slug`, `clamp_channel_fields`, глобальный `error_handler` (`app.add_error_handler`).
  Авто-детект архетипа+источника при `/add` (оба пути) + показ в сообщении.
  Планировщик (`on_startup`, APScheduler UTC): `poster.tick` CronTrigger(minute=0); `run_top_up_cycle` CronTrigger(minute=30, max_instances=1). **Ночной батч 03:00 УБРАН.**
- **`ui.py`** — inline-меню; настройки канала с кнопками **🎭 Стиль (выбор архетипа)** и **🔎 Источник тем (RSS↔поиск)**; `_save_channel` тоже клампит поля.
- **`rss_parser.py`**, **`web_scraper.py`** (Reddit/Medium RSS через Claude) — источники тем.
- **`wb_parser.py` / `wb_partner_parser.py`** — WB маркетплейс (отдельный пайплайн).
- **`scripts/`** — `deploy_fixes.py`, `ssh_test.py` (диагностика, поправлен), `restore_from_vps.py` + рабочие хелперы (clean_refusal_posts, clean_stale, set_cstokyo_search, run_userbot_step1/2 и т.д.).

### Каналы (6 активных)
| Канал | archetype | topic_source | image_source |
|---|---|---|---|
| @cstokyo2 | gaming_esports | search | stock |
| @Neffyi | gaming_casual | search | stock |
| @steam_arti | gaming_casual | search | auto |
| @hagenezykas | gaming_casual | rss | auto |
| @tftFunTime | default | rss | auto |
| @wblighter | (marketplace) | — | rss |

### Реализовано и задеплоено
Async-рефакторинг всех Claude-вызовов; error_handler; защита от инъекций (структурный промпт + лимиты полей);
busy_timeout; детектор мета-ответов/отказов + предфильтр повторяющихся тем; веб-поиск тем (+кэш);
архетипы/роутер/стиль/temperature/хуки; семантический дедуп (эмбеддинги); авто-детект архетипа+источника
на `/add` + редактирование кнопками в UI; бэкфилл архетипов существующим каналам; self-heal картинок +
якорь запроса к теме; ступенчатая генерация вместо ночного батча; аутентификация Telethon-юзербота.

---

## 2. Статус Telegram / Telethon

- **Bot API (постинг/алёрты):** `BOT_TOKEN` рабочий, бот крутится как systemd-сервис, постит в каналы где он админ. `ADMIN_CHAT_ID=615016161`.
- **Telethon (юзербот):** аутентифицирован как **Artur (@Artyr28Rus, id 615016161)**.
  - Сессия: `/opt/smm_bot/userbot.session` (chmod 600, только root). `*.session` в .gitignore.
  - telethon 1.43.2 в venv. Ключи TELEGRAM_API_ID/HASH/PHONE — в VPS `/opt/smm_bot/.env` (и в локальном .env). **Проверены живым входом — рабочие.**
  - Вход двухшаговый: `scripts/run_userbot_step1.py` (ставит telethon, шлёт код) → `scripts/run_userbot_step2.py <код> [2FA]` (завершает; код+2FA в ОДНОМ запуске). При «потраченном» коде — повторить step1.
  - ⚠️ **КОД БОТА ЮЗЕРБОТА ЕЩЁ НЕ ИСПОЛЬЗУЕТ** — пока только аутентификация. Сессия готова для подключения фич.
  - Гигиена: 2FA-пароль засветился в чате прошлой сессии — рекомендовано сменить облачный 2FA-пароль (на сессию не влияет).
- Секреты (телефон/2FA/хэш/коды) в память/файлы проекта НЕ сохранялись (кроме штатного .env).

---

## 3. Утверждённый план на будущее

**Фичи на юзерботе (Telethon) — приоритет:**
1. **Чтение истории канала** через user API (Bot API не умеет) → авто-добавление: handle → бот сам читает посты → `classify_channel`/`analyze` → готовая карточка. Это база для Q2/Q3 ниже.
2. **Добавление канала пересылкой поста** (forward_from_chat → handle+название; для постинга всё равно нужен бот-админ).
3. **Массовое добавление** (`/bulk_add` список handle или файл; авто-архетип+источник; карточки inactive до подтверждения админки).
4. Мониторинг рекламы (РСЯ-слой), авто-вступление.

**Управление масштабом (40 каналов на подходе):**
5. **Папки/группы** каналов (поле `group` в карточке + фильтр в UI + групповые действия).
6. **Ручные посты + смешанный режим** (свой текст/картинка в очередь или сразу).
7. **Режим модерации на канал** (авто-публикация vs ревью).

**Качество/аналитика (позже):**
8. Аналитика (просмотры/прирост → обратная связь в format_bias), медиа-библиотека, кросс-постинг,
   дашборд расходов, бренд-чек, A/B, веб-панель помимо бота, командные роли.
9. Дедуп: ререайт-вместо-skip при дубле; межканальный семантический дедуп.
10. Новый архетип `tv_series/fandom` для турецких сериалов (сейчас маппится в celeb_drama).
    Вывод: для сериальных каналов делать «новости/обсуждение», а не нарезки — тогда картинки проще.

**Архитектурные решения (утверждены):**
- Дедуп — ЛОКАЛЬНЫЕ эмбеддинги (выбрано), БД — SQLite (пока), генерация — ступенчатая (не ночной батч).
- Веб-поиск тем — основной для новостных ниш, RSS как фолбэк; кэш тем для экономии поисков.

**Переезд на другой VPS (если понадобится) — просто, всё в файлах:**
1. Новый VPS, python3.12 + venv + `pip install -r requirements.txt` + sentence-transformers torch telethon fal-client.
2. Скопировать `/opt/smm_bot` целиком: код, `channels/*.json`, `.env`, `data/content_factory.db`, `userbot.session`.
3. Пересоздать systemd `smm_bot.service`, добавить swap 4 ГБ.
4. Обновить `VPS_HOST`/`VPS_USER`/`VPS_KEY` в локальном `.env` (deploy_fixes.py, restore_from_vps.py,
   ssh_test.py читают их оттуда). Скрипты в `scripts/` — локальный одноразовый тулинг, в git не входят.
5. SQLite и `userbot.session` переносятся как файлы; перелогин Telethon обычно не нужен.
6. Обновить пути в памяти проекта.

---

## 4. Точка остановки (с чего начать в новом чате)

- **Последнее сделанное:** аутентификация Telethon-юзербота (работает, сессия создана). Ничего не сломано, сервис `active (running)`.
- **Открытый вопрос пользователя:** возможный переезд на другой VPS (ответ/чеклист — см. п.3, реализация по запросу).
- **Рекомендуемый старт:** реализовать **фичу №1 — чтение истории канала через юзербота** и на её базе авто/массовое добавление каналов (handle → чтение постов → классификация → карточка). Это разблокирует подготовку к массовому запуску 40 каналов.
- Перед стартом новой сессии: прочитать `MEMORY.md` и связанные файлы памяти (project_smm_bot, project_content_router, project_web_search_and_injection, project_userbot, project_refactor_2026-05, feedback_coding, project_technical_decisions).
