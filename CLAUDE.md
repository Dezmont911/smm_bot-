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
- `content_generator.py` — дирижёр: `run_for_channel` (собирает ПУЛ кандидатов
  `min(max(target*3,10),target+25)`, генерит до target, картинка по image_source),
  `run_top_up_cycle`, `run_emergency`. `_collect_topics` (search → RSS → web_scraper → evergreen),
  `_get_used_topics` (published+ready+**skipped**, против повторов после очистки),
  `_is_duplicate` (семантич. дедуп). Картинка подбирается по СОДЕРЖАНИЮ поста (`image_basis`),
  не по сырому заголовку темы.
- `content_router.py` / `archetypes.py` — стиль/«личность» канала по архетипу (default,
  gaming_esports, gaming_casual, anime, news, auto, celeb_drama, finance): style, format_bias,
  temperature, hooks. `resolve()`, `pick_format()`, `pick_hook()`.
- `topic_search.py` — темы через web_search Claude (+кэш `topic_cache`, TTL 8ч).
- `rss_parser.py` / `web_scraper.py` — RSS-ленты и reddit/medium.
- `dedup.py` — локальные эмбеддинги (paraphrase-multilingual-MiniLM-L12-v2, 384-dim, ленивая загрузка).

**Публикация и медиа**
- `poster.py` — `tick()` (ежечасно публикует по `post_times_utc`), `_publish` (медиа-файл →
  WB-картинка → URL → текст; self-heal картинки через FLUX), `_send_local_media` (фото/видео
  из файла для референсов), `_download_wb_image` (WB CDN через прокси).
- `image_fetcher.py` — Reddit → Pexels → Unsplash; запрос строит Claude. Якорь к теме канала
  только если короткий (не предложение-описание).
- `image_generator.py` — FLUX через fal.ai (`fal-ai/flux/schnell`, ~$0.003).

**Каналы / Telethon / БД**
- `channel_analyzer.py` — `analyze_export`, `analyze_posts(name, posts, about)` (для @username),
  `classify_channel`, `normalize_meta`. Анализ игнорирует рекламные посты.
- `userbot_reader.py` — Telethon-юзербот: `read_channel(username)` (для авто-/add),
  `read_new_posts(username, after_id, limit, with_media, media_dir)` (для референсов, скачивает медиа).
- `reference_importer.py` — импорт постов доноров: `import_for_channel`/`import_all`,
  лёгкий фильтр рекламы (AD_MARKERS), перефраз по флагу.
- `buffer_manager.py` — очередь постов (`add`, `get_next`, `mark_published`, evergreen).
- `database.py` — схема (`posts` с image_url/parse_mode/embedding/**media_path/media_type**,
  channels, topic_cache, evergreen_topics, processed_ads, error_log). `init()` идемпотентен.
- `config.py` — `cfg` (dataclass из .env). CLAUDE_MODEL=claude-haiku-4-5-20251001 и пр.
- `ui.py` — inline-меню (карточка канала, настройки, очередь, расписание, картинки-тумблер,
  источники тем, референс-каналы).
- `wb_parser.py` / `wb_partner_parser.py` — маркетплейс WB (отдельный пайплайн).
- `scripts/` — рабочие хелперы и `_*`/тест-скрипты (одноразовые с префиксом `_`).

**Планировщик (bot.py on_startup, UTC):** poster.tick `:00`; run_top_up_cycle `:30`
(только просевшие каналы); reference_importer.import_all раз в день `08:00`.

---

## Модель карточки канала (channels/*.json)

Ключевые поля: `channel_id` (@handle), `name`, `topic`, `channel_type` (content|marketplace),
`archetype`, `topic_source` (search|rss), `post_times_utc` (часы UTC; пусто = нет расписания),
`schedule_disabled` (true = автопубликация выкл), `daily_posts_count`, `post_length`
(«20-30» = слова), `image_source` (auto|stock|ai|rss|none — ЕДИНОЕ правило выбора картинки),
`use_images` (легаси, логикой больше НЕ управляет), `rss_sources`, `web_sources`,
`evergreen_topics`, `reference_channels` ([{handle, rephrase, take_media, skip_ads, last_id}]).

---

## Важные решения и конвенции (не нарушать без причины)

- **Тон постов — единый человечный** (`_HUMAN_VOICE`): без шаблонных крючков
  («Знаете ли вы, что N%…»), канцелярита, выдуманной статистики. Per-channel «тон» из UI
  убран; в промпт не подаётся.
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
- **Референсы:** режим «как есть» основной — медиа 1:1, текст перефраз или как есть. Вотермарки
  НЕ снимаем (нет надёжного способа): либо как есть, либо ручная замена картинки на посте.
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
