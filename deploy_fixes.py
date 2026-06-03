"""deploy_fixes.py — Деплой изменений на VPS

Запуск: py deploy_fixes.py
"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import paramiko
from dotenv import load_dotenv

load_dotenv()

# Параметры VPS берём из .env (не хардкодим IP/пользователя в коде)
VPS_HOST = os.environ["VPS_HOST"]
VPS_USER = os.getenv("VPS_USER", "root")
VPS_KEY  = os.environ["VPS_KEY"]

LOCAL_BASE = r"C:\Projects\smm_bot\smm_bot"
REMOTE_BASE = "/opt/smm_bot"

FILES = [
    "bot.py",
    "ai_client.py",       # ВАЖНО: тут async-клиент, детектор мета-ответов, санитизация, стиль
    "config.py",
    "content_generator.py",
    "database.py",
    "buffer_manager.py",
    "poster.py",
    "ui.py",
    "wb_parser.py",
    "image_fetcher.py",
    "rss_parser.py",
    "image_generator.py",
    "channel_analyzer.py",
    "claude_helper.py",   # НОВЫЙ модуль — async-клиент Claude (без него bot.py не импортируется)
    "web_scraper.py",     # тоже переведён на claude_helper
    "topic_search.py",    # НОВЫЙ модуль — поиск тем через web_search Claude
    "archetypes.py",      # НОВЫЙ модуль — пресеты стиля по нишам
    "content_router.py",  # НОВЫЙ модуль — стратегия генерации под канал
    "dedup.py",           # НОВЫЙ модуль — семантический дедуп (эмбеддинги)
    "userbot_reader.py",  # НОВЫЙ модуль — чтение канала по @username через Telethon
    "reference_importer.py",  # НОВЫЙ модуль — импорт постов из каналов-доноров
    "accounts.py",        # НОВЫЙ модуль — SaaS: пользователи, инвайты, планы, доступ
]

# (локальный путь относительно LOCAL_BASE, удалённый путь относительно REMOTE_BASE, метка)
# Для деплоя кода JSON каналов НЕ трогаем — на VPS могут быть свежие правки из бота.
CHANNEL_FILES = []

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(VPS_HOST, port=22, username=VPS_USER,
               key_filename=VPS_KEY, timeout=15)

def run(cmd):
    _, o, e = client.exec_command(cmd, timeout=60)
    out = o.read().decode('utf-8', 'replace')
    err = e.read().decode('utf-8', 'replace')
    return out + (("\nSTDERR: " + err) if err.strip() else "")

# 1. Загружаем файлы
sftp = client.open_sftp()
for f in FILES:
    sftp.put(os.path.join(LOCAL_BASE, f), f"{REMOTE_BASE}/{f}")
    print(f"  ✅ {f}")
# Загружаем обновлённые JSON каналов
for local_rel, remote_rel, label in CHANNEL_FILES:
    local_path = os.path.join(LOCAL_BASE, *local_rel.split("/"))
    remote_path = f"{REMOTE_BASE}/{remote_rel}"
    sftp.put(local_path, remote_path)
    print(f"  ✅ {label}")
sftp.close()

# 2. Миграция БД (parse_mode колонка)
print("\nМиграция БД...")
migration_script = """
import sqlite3
conn = sqlite3.connect('/opt/smm_bot/data/content_factory.db')
cols = [r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()]
if 'parse_mode' not in cols:
    conn.execute("ALTER TABLE posts ADD COLUMN parse_mode TEXT DEFAULT 'Markdown'")
    print("  ✅ Колонка parse_mode добавлена")
else:
    print("  ℹ️  parse_mode уже есть")
if 'embedding' not in cols:
    conn.execute("ALTER TABLE posts ADD COLUMN embedding BLOB")
    print("  ✅ Колонка embedding добавлена")
else:
    print("  ℹ️  embedding уже есть")
for _col in ('media_path', 'media_type', 'tg_file_id'):
    if _col not in cols:
        conn.execute(f"ALTER TABLE posts ADD COLUMN {_col} TEXT")
        print(f"  ✅ Колонка {_col} добавлена")
    else:
        print(f"  ℹ️  {_col} уже есть")
# processed_ads.due_at — для персистентного РСЯ-перекрытия (переживает рестарт)
pcols = [r[1] for r in conn.execute("PRAGMA table_info(processed_ads)").fetchall()]
if 'due_at' not in pcols:
    conn.execute("ALTER TABLE processed_ads ADD COLUMN due_at TEXT")
    print("  ✅ processed_ads.due_at добавлена")
else:
    print("  ℹ️  processed_ads.due_at уже есть")
updated = conn.execute(
    "UPDATE posts SET parse_mode = 'HTML' WHERE (parse_mode IS NULL OR parse_mode = 'Markdown') AND content LIKE '%<b>%' AND status IN ('ready', 'pending_review')"
).rowcount
print(f"  ✅ Обновлено WB-постов: {updated}")
conn.commit()
conn.close()
"""
run(f"cat > /tmp/migrate.py << 'EOF'\n{migration_script}\nEOF")
print(run("/opt/smm_bot/venv/bin/python /tmp/migrate.py"))

# 3. Устанавливаем fal-client если ещё нет
print("Установка fal-client...")
print(run("/opt/smm_bot/venv/bin/pip install fal-client --quiet 2>&1 | tail -2"))

# 4. Перезапуск
print("Перезапуск сервиса...")
print(run("systemctl restart smm_bot && sleep 2 && systemctl status smm_bot --no-pager | head -10"))

client.close()
print("\n✅ Готово!")
