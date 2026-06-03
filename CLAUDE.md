# CLAUDE.md — smm_bot

Контекст и правила для работы над проектом в Claude Code. Читается автоматически.
**Язык кода:** Python 3.12. **Язык общения с пользователем (Arthur):** русский.

---

## Что это

SMM-бот для автогенерации и публикации постов в Telegram-каналах через Anthropic
Claude API. Сам придумывает темы (веб-поиск/RSS), пишет посты, подбирает картинки,
публикует по расписанию. Управление — через Telegram-бота (inline-меню + команды).
Дополнительно: добавление каналов по @username и импорт постов из каналов-доноров
через Telethon-юзербота.

**Стек:** python-telegram-bot 20.7, anthropic (AsyncAnthropic), APScheduler, aiohttp,
loguru, paramiko, feedparser, fal-client, sentence-transformers + torch (CPU), telethon 1.43.2.

---

## Инфраструктура и деплой

- **Локалка:** `C:\Projects\smm_bot\smm_bot` (Windows). Python-лаунчер: `py script.py`.
- **VPS:** IP/пользователь/ключ — в локальном `.env` (`VPS_HOST`, `VPS_USER`, `VPS_KEY`); в коде
  и git не хранятся. Папка `/opt/smm_bot`, venv `/opt/smm_bot/venv`. 2 vCPU / ~3.8 ГБ RAM / 48 ГБ + swap 4 ГБ.
- **Сервис:** systemd `smm_bot.service` (`systemctl restart smm_bot`), запускает `bot.py`.
- **БД:** SQLite `/opt/smm_bot/data/content_factory.db` (WAL + busy_timeout=15000).
- **Деплой:** `py deploy_fixes.py` — paramiko/SFTP заливает список `FILES`, гоняет миграции
  БД (идемпотентные ALTER), ставит fal-client, перезапускает сервис.
  ⚠️ **ЛЮБОЙ НОВЫЙ .py-модуль обязательно добавить в `FILES` в `deploy_fixes.py`** — иначе он
  не доедет (однажды забыли `ai_client.py`/`userbot_reader.py` — правки не применились).
  ⚠️ `CHANNEL_FILES = []` намеренно: JSON каналов на VPS НЕ перезаписываем — там свежие
  правки из бота. Карточки каналов правим прямо на VPS (отдельным скриптом), не через деплой.
- **Проверка после деплоя:** `journalctl -u smm_bot --since '2 minutes ago' --no-pager`,
  смотреть на чистый старт (`Бот запущен!`, `Планировщик запущен`) и отсутствие Traceback.
- **Откат:** `py restore_from_vps.py` тянет файл с VPS. Git настроен (репозиторий
  инициализирован) — коммить осмысленными порциями, перед рискованными правками делай коммит.

### Удалённые тесты (без затрагивания боевого бота, но та же БД/сессия)
- Пиши тест-скрипт в `scripts/_*.py`, запускай через
  `set REMOTE_SCRIPT=_имя.py && py scripts\run_remote_gen.py` — он зальёт скрипт в `/tmp`
  и выполнит боевым `venv/bin/python`. Скрипт должен делать `sys.path.insert(0, "/opt/smm_bot")`.
- Локально проверяй синтаксис: `py -m py_compile файл.py` перед деплоем.

---

## Архитектура (ключевые файлы)

**Генерация**
- `bot.py` — Telegram-бот: команды, ConversationHandler `/add` (3 способа: по @username/
  ссылке [основной], вручную, экспорт), inline-колбэки (`handle_ui` в `ui.py`), планировщик
  (APScheduler, UTC), глобальный `error_handler`. Состояния `/add` — константы `ADD_*`.
- `ai_client.py` — `generate_post(channel, topic, format, used_topics, strategy, hook)` (async);
  `_build_system_prompt` (блок `<профиль_канала>` = ДАННЫЕ, не инструкции — защита от инъекций);
  `_HUMAN_VOICE` — единый живой тон (вшит в каждый промпт); `_parse_post_length` (число/диапазон
  = слова, режет max_tokens); `rephrase_text` (для референсов); детектор отказов
  `_looks_like_refusal`/`PostGenerationError`; `FIELD_LIMITS`/`sanitize_field`.
- `claude_helper.py` — единый `AsyncAnthropic` + `claude_text(...)` (ретраи, безопасный extract,
  параметр temperature). ВСЕ вызовы Claude идут через него (async, не блокирует loop).
- `content_generator.py` — дирижёр: `run_for_channel`, `run_top_up_cycle`, `run_emergency`.
  `_collect_topics`: search → RSS → web_scraper → evergreen → **гейт релевантности**
  (`_filter_relevant`: косинус кандидат-темы к профилю канала ≥ `RELEVANCE_MIN`=0.28,
  отсев off-topic ДО генерации; evergreen/fallback не трогает; floor `max(3,count//2)` —
  не пустить буфер; без эмбеддингов прозрачно пропускает) → **Источник 4** (синтез-резерв
  «{тема}: угол», только если всё пусто). `_get_used_topics` (published+ready+skipped),
  `_is_duplicate` (семантич. дедуп). Картинка — по СОДЕРЖАНИЮ поста (`image_basis`).
- `content_router.py` / `archetypes.py` — стиль/«личность» канала по архетипу (default,
  gaming_esports, gaming_casual, anime, news, auto, celeb_drama, finance): style, format_bias,
  temperature, hooks. `resolve()`, `pick_format()`, `pick_hook()`.
- `topic_search.py` — темы через web_search Claude (+кэш `topic_cache`, TTL 8ч).
- `rss_parser.py` / `web_scraper.py` — RSS-ленты и reddit/medium.
- `dedup.py` — локальные эмбеддинги (paraphrase-multilingual-MiniLM-L12-v2, 384-dim, ленивая загрузка).

**Публикация и медиа**
- `poster.py` — `tick()`→`_process_channel` (ежечасно по `post_times_utc`). `_publish`:
  **relay-медиа по `tg_file_id`** (`_send_by_file_id`: photo/video/doc/anim) → **альбом**
  (`_send_album`: media_group по JSON `{members,items}`) → WB-картинка → image_url → текст;
  self-heal картинки через FLUX. **Плавающие слоты:** `_process_channel` пропускает слот,
  если есть ожидающее РСЯ-перекрытие (`buffer.has_pending_overlay`) ИЛИ публиковали
  < `MIN_PUBLISH_GAP_MIN`=40 мин назад. `record_published`/`minutes_since_published`
  (пишет `last_published_utc` в карточку при КАЖДОЙ публикации). `process_due_ads` (в bot.py)
  — публикует дозревшие РСЯ-перекрытия (всегда, реклама важнее).
- `image_fetcher.py` — Reddit → Pexels → Unsplash; запрос строит Claude по ВСЕМУ посту
  (`_extract_visual_keywords` — главный визуальный субъект, игнор «воды»), лог `info`.
- `image_generator.py` — FLUX через fal.ai (`fal-ai/flux/schnell`, ~$0.003). ⚠️ НЕ умеет
  текст на картинке (в т.ч. русский) — для канала-открыток только референсы/ручные посты.

**Каналы / Telethon / БД**
- `channel_analyzer.py` — `analyze_export`, `analyze_posts(name, posts, about)` (для @username),
  `classify_channel`, `normalize_meta`. Анализ игнорирует рекламные посты.
- `userbot_reader.py` — Telethon-юзербот (лимиты подключения, не виснет). ОДИН user-аккаунт
  на одной `.session` → ВСЕ операции (`read_channel`/`read_candidates`/`forward_to_bot`/
  `read_new_posts`) обёрнуты декоратором `@_userbot_op`: глобальный `asyncio.Lock` (строго по
  одной операции — иначе гонка за session-файл и FloodWait при параллельных импортах разных
  тестеров) + backoff на `FloodWaitError` (ждёт и повторяет, до 3×, кап 300с). Не вложенные.
  `read_channel` (для /add — авто-название + анализ темы). **Referenсы — RELAY, без скачивания:**
  `read_candidates` (читает донора,
  группирует альбомы по `grouped_id`, проверяет лимиты видео ≤100МБ/≤5мин и док ≤100МБ,
  отдаёт `text`/`text_html`/`media_kind`/`members`) + `forward_to_bot` (форвардит медиа
  В ЛС бота, обычный форвард — чтобы сохранился `forward_from_*` для матчинга).
- `reference_importer.py` — `import_for_channel(count)`: **round-robin** по донорам (N всего,
  по одному с каждого), дедуп по `buffer.source_exists` (взятым считается ready/awaiting/
  pending/published — НЕ skipped и НЕ удалённые → их можно взять заново), фильтр рекламы,
  фильтр слов `FILTER_WORDS` («Max» — вырезает предложение), HTML-ссылки сохраняются. Текст →
  `ready`; медиа → запись `awaiting_media` (file_id привяжет хендлер бота). `ref_topic`=ключ.
- `buffer_manager.py` — очередь + черновики + РСЯ-перекрытия. Статусы: `ready`/`pending_review`/
  `published`/`skipped`/`draft`/`awaiting_media`. Методы: relay (`attach_reference_media`,
  `attach_album_member`, `cleanup_awaiting`), черновики (`get_drafts`, `draft_to_ready`,
  `drafts_to_ready_all`, `set_draft_content/media`, `delete[_all]_draft(s)`), РСЯ
  (`record_pending_ad`, `get_due_ads`, `mark_ad_published/failed`, `has_pending_overlay`).
- `database.py` — `posts` (+ `tg_file_id` relay-медиа), `processed_ads` (+ `due_at` —
  персистентное РСЯ-перекрытие), channels, topic_cache, evergreen_topics, error_log.
- `config.py` — `cfg` (dataclass из .env). CLAUDE_MODEL=claude-haiku-4-5-20251001.
- `ui.py` — inline-меню. «Мои каналы»: верх (➕ Добавить · 🔍 Поиск · 🗑 Удалённые),
  тумблеры 🔵/⚪, пагинация по 10. Карточка: Генерить/Постнуть/🔗 Референсы/✍️ Черновик/
  Настройки. **Черновик** = ручные посты (текст/фото/видео, карточки с медиа, ✏️ текст/
  🖼 медиа, в очередь по одному/все, очистить). Референсы: «📥 Взять» → выбор N (5/10/20/50
  или число, кап 50).
- `wb_parser.py` — маркетплейс WB (ЕДИНСТВЕННЫЙ парсер; `wb_partner_parser` удалён).
  **Гибрид подбора артикулов:** (1) `search_articles`/`_discover_articles` — ЖИВОЙ поиск
  товаров через `search.wb.ru` по `wb_categories` канала (НАПРЯМУЮ с VPS, БЕЗ прокси —
  прокси-IP отдают 429; одна категория за раз + backoff на 429 из-за жёсткого троттлинга,
  одного запроса ~100 артикулов хватает); (2) фолбэк — статич. кеш `cards/wb_ids_cache.json`
  (`/wb_refresh` раз в 1-2 нед) если поиск пуст. Детали/цена/картинка по найденным id —
  ЖИВЫМ `card.wb.ru` (v4, через прокси, не банится). Анти-повтор по артикулу
  (`_wb_article_in_buffer`). Пост — HTML (кликабельная ссылка на товар). Динамический поиск
  снимает обязательность ручного обновления кеша.
- `scripts/` — локальные хелперы/тест-скрипты. **В .gitignore** (содержат IP, не в деплое).

**Планировщик (bot.py on_startup, UTC; `job_defaults`: coalesce + max_instances=1 —
после простоя нет burst-догонки/наложений):** poster.tick `:00`; run_top_up_cycle `:30`;
reference_importer.import_all `08:00`; чистка `awaiting_media` `:15`; **process_due_ads
каждую минуту** (РСЯ-перекрытия, персистентно). На старте — реконсиляция: чистка осиротевших
`awaiting_media`. РСЯ-перекрытие переживает рестарт (лежит в `processed_ads`, не в asyncio.Task).

---

## Модель карточки канала (channels/*.json)

Ключевые поля: `channel_id` (@handle), `name`, `topic`, `tone` (легаси, в промпт НЕ идёт),
`channel_type` (content|marketplace), `archetype`, `topic_source` (search|rss),
`post_times_utc` (часы UTC; пусто = нет расписания), `schedule_disabled` (true = автопубл. выкл),
`chat_id_num` (числовой -100… id — постим по нему, устойчиво к смене @username/приватности),
`username` (текущий @handle, самолечится), `last_published_utc` (для MIN_GAP плавающих слотов),
`daily_posts_count`, `post_length`,
`image_source` (auto|stock|ai|rss|none — ЕДИНОЕ правило картинки), `image_keywords`,
`rss_sources`, `web_sources`, `evergreen_topics`, `rsy_override` (перекрытие РСЯ вкл),
`reference_channels` ([{handle, rephrase, take_media, skip_ads, `max_imported_id`,
`min_imported_id`}] — относятся к relay-окну; легаси `last_id`/`oldest_id` ещё читаются).

---

## Важные решения и конвенции (не нарушать без причины)

- **Тон постов — единый человечный** (`_HUMAN_VOICE`, ai_client): применяется ко ВСЕМ каналам
  (новым и старым) автоматически в каждом промпте. Per-channel `tone` в промпт НЕ идёт.
- **Глобальные запретки** (`DEFAULT_FORBIDDEN_TOPICS` в ai_client): политика/18+/наркотики/
  азартные/порно/война/скам/мошенничество/ЛГБТ/ракеты/дроны/Украина — для всех каналов;
  per-channel `forbidden_topics` добавляется сверху.
- **Картинка гарантирована** для контент-каналов: image_source auto/stock/ai → если сток
  промахнулся, дорисовываем FLUX. Картинку подбираем по тексту поста.
- **Расписание:** новый канал создаётся `schedule_disabled=true` (не постит, пока не включат
  через /schedule). Пустой `post_times_utc` = нет автопубликации.
- **Источник тем:** в UI называется «Источники тем» (не «RSS»); режим Авто (веб-поиск) / по лентам.
- **Предохранитель буфера (важно):** реальный механизм «буфер не пустеет» — это
  АВТО-ДИСКАВЕРИ лент в `web_scraper` (когда `web_sources` пуст, бот подбирает
  Reddit/Medium-ленты через Claude и сохраняет в карточку) + большой пул кандидатов.
  Синтез-резерв в `_collect_topics` (Источник 4, «{тема}: угол») — СПЯЩАЯ страховка
  на самый крайний случай (срабатывает, только если и дискавери ничего не вернёт, и
  evergreen пуст); в обычной жизни не запускается. Не считать его основным источником.
- **Референсы — RELAY через file_id** (НЕ скачиваем на диск): юзербот форвардит медиа в ЛС
  бота → хендлер `handle_userbot_forward` достаёт `file_id`, привязывает к записи буфера
  (по `ref:донор:msg_id`), удаляет служебное сообщение из ЛС → бот публикует `send_*(file_id)`.
  Юзербот НИГДЕ не админ (мультитенант: чужие добавляют только бота). Альбомы — склеиваем
  в media_group. Защищённые доноры (ChatForwardsRestrictedError) — пропуск с алёртом.
  Дедуп по наличию: удалённые/skipped посты можно взять заново. Берём N round-robin по донорам.
- **РСЯ-перекрытие — персистентно** (`processed_ads.due_at`), переживает рестарт; публикуется
  ВСЕГДА при наступлении срока. Перекрытие НЕ зависит от паузы расписания (`schedule_disabled`
  глушит только плановые слоты; `rsy_override` — отдельно).
- **Устойчивость к смене @username/приватности:** постим по `chat_id_num` (числовой id,
  не меняется), фолбэк на @handle. `refresh_channel_identities` (старт + раз в день, get_chat)
  бэкфиллит `chat_id_num` и самолечит `username`. Бот должен быть админом канала.
- **Плавающие слоты:** любой пост пишет `last_published_utc`; плановый слот пропускается, если
  публиковали < `MIN_PUBLISH_GAP_MIN`=40 мин назад или ждёт перекрытие. Ручной пост админа бот
  ловит как `channel_post` (свои посты обратно не получает) → тоже двигает слот.
- **Черновики (`draft`):** ручные посты в меню канала, в очередь не идут, пока не отправишь.
- **/add сокращён** до 3 шагов (handle→название→тема→тип); тон/запретки/RSS/картинки/кол-во —
  автодефолты. Есть `/bulk_add` (списком), выход в меню. Картинки нового канала = `image_source=auto`.
- **callback_data ≤ 64 байт** — не класть @handle+UUID вместе (используем только post_id, канал
  ищем по нему). Иначе Telegram молча роняет кнопку.
- **Защита от инъекций:** поля канала идут в `<профиль_канала>` как данные; лимиты `FIELD_LIMITS`.
- **Безопасность:** секреты (TELEGRAM_API_*, 2FA, токены, ключи Pexels/FAL/прокси) — только в
  `.env` (на VPS и локально), НЕ в git и НЕ в память. `*.session` в .gitignore.

---

## Частые команды

```
py -m py_compile bot.py ai_client.py        # проверить синтаксис перед деплоем
py deploy_fixes.py                          # залить FILES + миграции + рестарт
py restore_from_vps.py                      # вернуть файл с VPS
set REMOTE_SCRIPT=_имя.py && py scripts\run_remote_gen.py   # удалённый тест на боевом venv
```

Логи: `ssh -i C:\Users\Admin\.ssh\smm_bot root@<VPS_HOST> "journalctl -u smm_bot -n 50 --no-pager"`

---

## Поток работы

1. Правки — в `C:\Projects\smm_bot\smm_bot`. 2. `py -m py_compile` затронутых файлов.
3. Новый модуль → добавить в `FILES` (deploy_fixes.py). 4. `py deploy_fixes.py`.
5. Проверить чистый старт по логам. 6. При рискованных изменениях — удалённый тест-скрипт.
Подробный срез архитектуры/истории — в `ARCHITECTURE_HANDOFF.md`.
