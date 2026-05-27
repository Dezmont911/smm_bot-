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
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # строки как словари: row["column"]
        conn.execute("PRAGMA journal_mode=WAL")  # лучше для конкурентного доступа
        conn.execute("PRAGMA foreign_keys=ON")   # проверка внешних ключей
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
                    response_post_id TEXT,               -- какой пост опубликовали в ответ
                    published_at     TEXT,               -- ISO timestamp или NULL
                    status           TEXT DEFAULT 'detected'  -- detected/published/failed
                );

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
