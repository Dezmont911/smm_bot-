"""
database.py — Подключение к базе данных и создание таблиц

Сейчас используем SQLite — встроен в Python, ничего устанавливать не надо.
На VPS просто меняем одну строку в .env: DATABASE_URL=postgresql://...
и переключаемся на PostgreSQL.

Схема таблиц взята из handbook (Слой 3 и Слой 4).

Использование:
    from database import db
    db.init()           # создать таблицы (один раз при старте)
    conn = db.connect() # получить соединение для запросов
"""

import sqlite3
from pathlib import Path

from loguru import logger


# Путь к файлу БД (создаётся автоматически рядом со скриптом)
DB_PATH = Path(__file__).parent / "data" / "content_factory.db"


class Database:
    """Обёртка над SQLite с удобными методами."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Открывает соединение с БД. Всегда возвращает строки как словари."""
        # timeout=15 — ждём освобождения блокировки на уровне драйвера sqlite3
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row  # строки как словари: row["column"]
        conn.execute("PRAGMA journal_mode=WAL")     # лучше для конкурентного доступа
        conn.execute("PRAGMA foreign_keys=ON")      # проверка внешних ключей
        conn.execute("PRAGMA busy_timeout=15000")   # ждать 15с при 'database is locked'
        return conn

    def init(self):
        """
        Создаёт все таблицы если их нет.
        Вызывается один раз при старте системы.
        """
        logger.info(f"Инициализация БД: {self.db_path}")

        with self.connect() as conn:
            conn.executescript("""
                -- --------------------------------------------------------
                -- Карточки каналов
                -- --------------------------------------------------------
                CREATE TABLE IF NOT EXISTS channels (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_handle   TEXT UNIQUE NOT NULL,   -- @mychannel
                    name        TEXT NOT NULL,           -- "Финансы для людей"
                    topic       TEXT,                    -- тема канала
                    tone        TEXT,                    -- тон общения
                    config_json TEXT,                    -- полный JSON карточки
                    active      INTEGER DEFAULT 1        -- 1=активен, 0=выключен
                );

                -- --------------------------------------------------------
                -- Буфер постов (Слой 3 из handbook)
                -- --------------------------------------------------------
                CREATE TABLE IF NOT EXISTS posts (
                    id           TEXT PRIMARY KEY,       -- UUID
                    channel_id   TEXT NOT NULL,          -- @mychannel
                    content      TEXT NOT NULL,          -- текст поста
                    format       TEXT,                   -- совет/факт/вопрос/разбор/инфоповод
                    topic        TEXT,                   -- инфоповод/тема для этого поста
                    status       TEXT DEFAULT 'ready',   -- ready / pending_review / published / skipped
                    image_url    TEXT,                   -- URL картинки (из RSS или Unsplash) или NULL
                    parse_mode   TEXT DEFAULT 'Markdown', -- Markdown / HTML (для WB-постов)
                    embedding    BLOB,                    -- вектор поста (float32) для семантич. дедупа
                    media_path   TEXT,                    -- локальный файл медиа (легаси-референс) или NULL
                    media_type   TEXT,                    -- photo / video / document / animation / NULL
                    tg_file_id   TEXT,                    -- file_id медиа в Telegram (relay-референс, без скачивания)
                    generated_at TEXT NOT NULL,          -- ISO timestamp
                    published_at TEXT,                   -- ISO timestamp или NULL
                    FOREIGN KEY (channel_id) REFERENCES channels(tg_handle)
                );

                -- --------------------------------------------------------
                -- Рекламные посты Яндекса (Слой 4 из handbook)
                -- --------------------------------------------------------
                CREATE TABLE IF NOT EXISTS processed_ads (
                    id               TEXT PRIMARY KEY,   -- UUID
                    channel_id       TEXT NOT NULL,      -- @mychannel
                    ad_message_id    INTEGER NOT NULL,   -- ID сообщения в Telegram
                    detected_at      TEXT NOT NULL,      -- ISO timestamp
                    due_at           TEXT,               -- когда публиковать перекрытие (ISO, переживает рестарт)
                    response_post_id TEXT,               -- какой пост опубликовали в ответ
                    published_at     TEXT,               -- ISO timestamp или NULL
                    status           TEXT DEFAULT 'detected'  -- detected/published/failed/expired
                );

                -- --------------------------------------------------------
                -- Кэш тем из веб-поиска (чтобы не искать на каждый прогон)
                -- --------------------------------------------------------
                CREATE TABLE IF NOT EXISTS topic_cache (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id  TEXT NOT NULL,           -- @mychannel
                    topic       TEXT NOT NULL,           -- текст темы
                    created_at  TEXT NOT NULL,           -- ISO timestamp (для TTL)
                    used        INTEGER DEFAULT 0        -- 0=свободна, 1=уже взята в пост
                );

                CREATE INDEX IF NOT EXISTS idx_topic_cache_channel
                    ON topic_cache(channel_id, used);

                -- --------------------------------------------------------
                -- Банк вечнозелёных тем (резерв когда RSS пустой)
                -- --------------------------------------------------------
                CREATE TABLE IF NOT EXISTS evergreen_topics (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id  TEXT NOT NULL,           -- @mychannel
                    topic       TEXT NOT NULL,           -- текст темы
                    last_used_at TEXT,                   -- когда последний раз использовалась
                    use_count   INTEGER DEFAULT 0        -- сколько раз использовалась
                );

                -- --------------------------------------------------------
                -- Лог ошибок (для дебага и алертов)
                -- --------------------------------------------------------
                CREATE TABLE IF NOT EXISTS error_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id  TEXT,                    -- @mychannel или NULL (общая ошибка)
                    error_type  TEXT NOT NULL,           -- generation/posting/monitoring/rss
                    message     TEXT NOT NULL,           -- описание ошибки
                    occurred_at TEXT NOT NULL,           -- ISO timestamp
                    resolved    INTEGER DEFAULT 0        -- 0=открыта, 1=решена
                );

                -- --------------------------------------------------------
                -- SaaS: пользователи (тестеры) и инвайт-коды
                -- --------------------------------------------------------
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,   -- Telegram id
                    plan        TEXT DEFAULT 'trial',  -- trial | free | pro | admin
                    trial_until TEXT,                  -- ISO; план trial действует до этой даты
                    invited_by  TEXT,                  -- код инвайта или id пригласившего
                    created_at  TEXT,
                    note        TEXT
                );

                CREATE TABLE IF NOT EXISTS invite_codes (
                    code        TEXT PRIMARY KEY,
                    plan        TEXT DEFAULT 'trial',  -- какой план выдаёт
                    days        INTEGER DEFAULT 30,    -- на сколько дней trial
                    max_uses    INTEGER DEFAULT 1,
                    used_count  INTEGER DEFAULT 0,
                    active      INTEGER DEFAULT 1,
                    created_by  INTEGER,
                    created_at  TEXT
                );

                -- --------------------------------------------------------
                -- Учёт расходов на платные сервисы (Claude, fal.ai)
                -- Пишется на каждый вызов; для просмотра «сколько потрачено»
                -- за период (сегодня / 7 / 30 дней / всё время / произвольно).
                -- --------------------------------------------------------
                CREATE TABLE IF NOT EXISTS usage_costs (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts            TEXT NOT NULL,        -- UTC '%Y-%m-%dT%H:%M:%S'
                    service       TEXT NOT NULL,        -- 'claude' | 'fal'
                    model         TEXT,                 -- модель / endpoint
                    purpose       TEXT,                 -- generate/analyze/topic/image/...
                    input_tokens  INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    units         INTEGER DEFAULT 0,    -- кол-во картинок (fal)
                    cost_usd      REAL NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_usage_costs_ts ON usage_costs(ts);

                -- --------------------------------------------------------
                -- Boost subsystem: separate manually tracked channels and dry-run events
                -- --------------------------------------------------------
                CREATE TABLE IF NOT EXISTS boost_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    boost_enabled INTEGER NOT NULL DEFAULT 0,
                    boost_dry_run INTEGER NOT NULL DEFAULT 1,
                    real_orders_enabled INTEGER NOT NULL DEFAULT 0,
                    default_quantity INTEGER NOT NULL DEFAULT 500,
                    default_service_id TEXT,
                    provider TEXT NOT NULL DEFAULT 'twiboost',
                    last_error TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS boost_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_key TEXT UNIQUE NOT NULL,
                    smm_channel_id TEXT,
                    owner_id INTEGER,
                    tg_chat_id TEXT,
                    username TEXT,
                    title TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    quantity INTEGER,
                    service_id TEXT,
                    note TEXT,
                    last_seen_message_id INTEGER,
                    last_order_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS boost_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    boost_channel_id INTEGER NOT NULL,
                    tg_chat_id TEXT,
                    message_id INTEGER NOT NULL,
                    event_key TEXT,
                    media_group_id TEXT,
                    canonical_message_id INTEGER,
                    event_type TEXT NOT NULL DEFAULT 'post',
                    post_url TEXT,
                    quantity INTEGER NOT NULL,
                    service_id TEXT,
                    provider_order_id TEXT,
                    status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    reason_code TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (boost_channel_id) REFERENCES boost_channels(id)
                );

                CREATE INDEX IF NOT EXISTS idx_boost_channels_enabled
                    ON boost_channels(enabled);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_boost_channels_smm_channel
                    ON boost_channels(smm_channel_id);

                CREATE INDEX IF NOT EXISTS idx_boost_orders_channel
                    ON boost_orders(boost_channel_id, message_id);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_boost_orders_unique_event
                    ON boost_orders(boost_channel_id, event_key, COALESCE(service_id, ''));

                -- --------------------------------------------------------
                -- Индексы для быстрых запросов
                -- --------------------------------------------------------
                CREATE INDEX IF NOT EXISTS idx_posts_channel_status
                    ON posts(channel_id, status);

                CREATE INDEX IF NOT EXISTS idx_processed_ads_channel
                    ON processed_ads(channel_id, status);

                CREATE INDEX IF NOT EXISTS idx_evergreen_channel
                    ON evergreen_topics(channel_id);
            """)

        logger.success(f"БД готова: {self.db_path}")


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР — импортируй во всех модулях:
#   from database import db
# ============================================================
db = Database()


# ============================================================
# ТЕСТ — запускается напрямую: python database.py
# ============================================================
if __name__ == "__main__":
    print("🗄️  Тест базы данных\n")

    # Создаём таблицы
    db.init()

    # Проверяем что таблицы созданы
    with db.connect() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()

        print(f"✅ Создано таблиц: {len(tables)}")
        for table in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table['name']}").fetchone()[0]
            print(f"   📋 {table['name']:25s} — {count} записей")

    print(f"\n📁 Файл БД: {db.db_path}")
    print(f"   Размер: {db.db_path.stat().st_size} байт")
    print("\n✅ База данных работает!")
