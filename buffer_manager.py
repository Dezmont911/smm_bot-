"""
buffer_manager.py — Управление буфером постов (Слой 3 из handbook)

Буфер — это сердце системы. Здесь хранятся готовые посты,
ожидающие публикации. Все правила из handbook реализованы здесь.

Золотое правило (handbook): буфер никогда не должен опустеть.
Минимум — 8 постов на канал (запас на 2 дня).

Использование:
    from buffer_manager import buffer
    level = buffer.get_level("@mychannel")      # сколько постов в очереди
    post  = buffer.get_next("@mychannel")        # взять следующий пост
    buffer.add(post_dict)                        # добавить пост в очередь
"""

import uuid
from datetime import datetime, timezone

from loguru import logger

from config import cfg
from database import db


class BufferManager:
    """Все операции с буфером постов."""

    # Уровни буфера из handbook
    LEVEL_OK        = cfg.BUFFER_MIN        # 8 постов — норма
    LEVEL_EMERGENCY = cfg.BUFFER_EMERGENCY  # 4 поста — запустить генерацию
    LEVEL_CRITICAL  = cfg.BUFFER_CRITICAL   # 2 поста — алерт администратору

    # --------------------------------------------------------
    # Добавление постов
    # --------------------------------------------------------

    def add(self, post: dict) -> str:
        """
        Добавляет один пост в буфер.

        Аргументы:
            post — словарь из ai_client.generate_post():
                   {"content": "...", "format": "совет",
                    "channel_id": "@mychannel", "topic": "..."}

        Возвращает: post_id (UUID)
        """
        post_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Берём статус из поста или ставим ready по умолчанию
        status = post.get("status", "ready")

        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO posts (id, channel_id, content, format, topic, status, generated_at, image_url, parse_mode, embedding, media_path, media_type, tg_file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_id,
                    post["channel_id"],
                    post["content"],
                    post.get("format", ""),
                    post.get("topic", ""),
                    status,
                    now,
                    post.get("image_url"),
                    post.get("parse_mode", "Markdown"),
                    post.get("embedding_blob"),
                    post.get("media_path"),
                    post.get("media_type"),
                    post.get("tg_file_id"),
                ),
            )

        logger.debug(f"Пост добавлен в буфер | {post['channel_id']} | {post_id[:8]}...")
        return post_id

    def add_batch(self, posts: list[dict]) -> list[str]:
        """
        Добавляет список постов в буфер.
        Используется при утренней генерации.
        """
        ids = []
        for post in posts:
            post_id = self.add(post)
            ids.append(post_id)
        logger.info(f"Добавлено {len(ids)} постов в буфер для {posts[0]['channel_id']}")
        return ids

    def attach_reference_media(self, topic: str, file_id: str, media_type: str) -> bool:
        """
        Привязывает file_id к ожидающей записи (relay-референс).

        Бот получил пересланное медиа в ЛС, вытащил file_id — записываем его в
        пост со статусом 'awaiting_media' (матчим по topic = ref:донор:msg_id) и
        переводим пост в 'ready' (готов к публикации по расписанию).
        Возвращает True, если нашлась и обновилась ожидающая запись.
        """
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM posts WHERE topic = ? AND status = 'awaiting_media' "
                "AND (media_type IS NULL OR media_type != 'album') "
                "ORDER BY generated_at ASC LIMIT 1",
                (topic,),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE posts SET tg_file_id = ?, media_type = ?, status = 'ready' WHERE id = ?",
                (file_id, media_type, row["id"]),
            )
        return True

    def attach_album_member(self, topic_prefix: str, member_id: int, file_id: str, media_type: str) -> bool:
        """
        Привязывает file_id к одному кадру альбома (relay).

        Альбомная запись (media_type='album', status='awaiting_media') хранит в
        tg_file_id JSON: {"members":[id,...], "items":{"id":{"file_id","type"}}}.
        Ищем ожидающий альбом донора (topic LIKE 'ref:донор:%'), у которого этот
        member_id есть в members и ещё не заполнен. Заполняем; когда собраны все
        кадры — переводим в 'ready'. Возвращает True, если member привязан.
        """
        import json
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT id, tg_file_id FROM posts WHERE status = 'awaiting_media' "
                "AND media_type = 'album' AND topic LIKE ?",
                (topic_prefix + "%",),
            ).fetchall()
            for r in rows:
                try:
                    data = json.loads(r["tg_file_id"] or "{}")
                except Exception:
                    continue
                members = data.get("members", [])
                items = data.get("items", {})
                if member_id in members and str(member_id) not in items:
                    items[str(member_id)] = {"file_id": file_id, "type": media_type}
                    data["items"] = items
                    done = len(items) >= len(members)
                    conn.execute(
                        "UPDATE posts SET tg_file_id = ?, status = ? WHERE id = ?",
                        (json.dumps(data), "ready" if done else "awaiting_media", r["id"]),
                    )
                    return True
        return False

    def cleanup_awaiting(self, older_than_minutes: int = 60) -> int:
        """
        Удаляет зависшие записи 'awaiting_media' (медиа так и не пришло от юзербота).
        Удаление, а не пометка — чтобы пост можно было импортировать заново
        (дедуп по topic иначе навсегда бы его заблокировал). Возвращает число удалённых.
        """
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
        with db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM posts WHERE status = 'awaiting_media' AND generated_at < ?",
                (cutoff,),
            )
            n = cur.rowcount
        if n:
            logger.info(f"Очистка зависших awaiting_media: удалено {n}")
        return n

    def source_exists(self, channel_id: str, topic: str) -> bool:
        """
        Есть ли у нас этот исходный пост СЕЙЧАС (в очереди или уже опубликован)?

        topic — однозначный ключ исходного сообщения (ref:донор:msg_id).
        Считаем «взятым» только активные/опубликованные статусы. Статус 'skipped'
        (удалён через /review) и удалённые строки (очистка буфера) НЕ блокируют —
        такие посты можно взять заново (по факту они не публиковались).
        """
        with db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM posts WHERE channel_id = ? AND topic = ? "
                "AND status IN ('ready', 'awaiting_media', 'pending_review', 'published') "
                "LIMIT 1",
                (channel_id, topic),
            ).fetchone()
        return row is not None

    # --------------------------------------------------------
    # Получение постов
    # --------------------------------------------------------

    def get_next(self, channel_id: str) -> dict | None:
        """
        Берёт следующий пост из буфера (самый старый со статусом 'ready').
        НЕ меняет статус — это делает mark_published() после успешной публикации.

        Возвращает словарь с данными поста или None если буфер пуст.
        """
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM posts
                WHERE channel_id = ? AND status = 'ready'
                ORDER BY generated_at ASC
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()

        if row is None:
            logger.warning(f"Буфер пуст! | канал: {channel_id}")
            return None

        return dict(row)

    def get_level(self, channel_id: str) -> int:
        """
        Возвращает количество постов в буфере (ready + pending_review).
        Учитываем оба статуса — иначе генератор будет гнать дубли
        пока посты ждут одобрения администратора.
        """
        with db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE channel_id = ? AND status IN ('ready', 'pending_review')",
                (channel_id,),
            ).fetchone()[0]
        return count

    def get_ready_count(self, channel_id: str) -> int:
        """Только одобренные посты (статус ready) — готовы к публикации."""
        with db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE channel_id = ? AND status = 'ready'",
                (channel_id,),
            ).fetchone()[0]
        return count

    def get_levels_all(self) -> dict[str, int]:
        """
        Возвращает уровни буфера для ВСЕХ активных каналов.
        Используется для дашборда и утренней проверки.
        Формат: {"@channel1": 12, "@channel2": 3, ...}
        """
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT channel_id, COUNT(*) as cnt
                FROM posts
                WHERE status = 'ready'
                GROUP BY channel_id
                """,
            ).fetchall()

            # Добавляем каналы с нулевым буфером
            all_channels = conn.execute(
                "SELECT tg_handle FROM channels WHERE active = 1"
            ).fetchall()

        levels = {row["channel_id"]: row["cnt"] for row in rows}
        for ch in all_channels:
            if ch["tg_handle"] not in levels:
                levels[ch["tg_handle"]] = 0

        return levels

    # --------------------------------------------------------
    # Обновление статусов
    # --------------------------------------------------------

    def mark_published(self, post_id: str):
        """Помечает пост как опубликованный после успешной публикации."""
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as conn:
            conn.execute(
                "UPDATE posts SET status = 'published', published_at = ? WHERE id = ?",
                (now, post_id),
            )
        logger.debug(f"Пост опубликован | {post_id[:8]}...")

    def mark_skipped(self, post_id: str):
        """Помечает пост как пропущенный (например, плохое качество)."""
        with db.connect() as conn:
            conn.execute(
                "UPDATE posts SET status = 'skipped' WHERE id = ?",
                (post_id,),
            )
        logger.debug(f"Пост пропущен | {post_id[:8]}...")

    def approve_post(self, post_id: str):
        """
        Одобряет пост — переводит из pending_review в ready.
        Вызывается когда админ нажимает ✅ в боте.
        """
        with db.connect() as conn:
            conn.execute(
                "UPDATE posts SET status = 'ready' WHERE id = ? AND status = 'pending_review'",
                (post_id,),
            )
        logger.info(f"Пост одобрен | {post_id[:8]}...")

    def approve_post_with_image(self, post_id: str, image_url: str):
        """
        Одобряет пост и прикрепляет картинку.
        Вызывается когда админ нажимает 🖼 + Картинка в боте.
        """
        with db.connect() as conn:
            conn.execute(
                "UPDATE posts SET status = 'ready', image_url = ? WHERE id = ?",
                (image_url, post_id),
            )
        logger.info(f"Пост одобрен с картинкой | {post_id[:8]}...")

    def get_pending_review(self, channel_id: str | None = None) -> list[dict]:
        """
        Возвращает посты ожидающие одобрения администратора.
        Если channel_id=None — возвращает для всех каналов.
        """
        with db.connect() as conn:
            if channel_id:
                rows = conn.execute(
                    "SELECT * FROM posts WHERE channel_id = ? AND status = 'pending_review' ORDER BY generated_at ASC",
                    (channel_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM posts WHERE status = 'pending_review' ORDER BY generated_at ASC"
                ).fetchall()
        return [dict(row) for row in rows]

    def update_content(self, post_id: str, new_content: str):
        """
        Обновляет текст поста (когда админ нажимает ✏️ Изменить).
        После редактирования пост автоматически одобряется.
        """
        with db.connect() as conn:
            conn.execute(
                "UPDATE posts SET content = ?, status = 'ready' WHERE id = ?",
                (new_content, post_id),
            )
        logger.info(f"Пост отредактирован и одобрен | {post_id[:8]}...")

    # --------------------------------------------------------
    # Проверка уровня и статус буфера
    # --------------------------------------------------------

    def check_status(self, channel_id: str) -> str:
        """
        Возвращает статус буфера для канала:
            'ok'        — всё хорошо (>= LEVEL_OK постов)
            'low'       — мало (< LEVEL_OK, но >= LEVEL_EMERGENCY)
            'emergency' — критично мало (< LEVEL_EMERGENCY) → нужна генерация
            'critical'  — опасно (< LEVEL_CRITICAL) → алерт администратору
        """
        level = self.get_level(channel_id)

        if level < self.LEVEL_CRITICAL:
            return "critical"
        elif level < self.LEVEL_EMERGENCY:
            return "emergency"
        elif level < self.LEVEL_OK:
            return "low"
        else:
            return "ok"

    def needs_generation(self, channel_id: str) -> bool:
        """
        Нужна ли генерация для этого канала?
        True если постов меньше минимума (LEVEL_EMERGENCY).
        """
        return self.get_level(channel_id) < self.LEVEL_EMERGENCY

    # --------------------------------------------------------
    # Вечнозелёные темы (резерв)
    # --------------------------------------------------------

    def add_evergreen_topics(self, channel_id: str, topics: list[str]):
        """Добавляет вечнозелёные темы для канала (из карточки канала)."""
        with db.connect() as conn:
            for topic in topics:
                # Добавляем только если такой темы ещё нет
                exists = conn.execute(
                    "SELECT 1 FROM evergreen_topics WHERE channel_id = ? AND topic = ?",
                    (channel_id, topic),
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO evergreen_topics (channel_id, topic) VALUES (?, ?)",
                        (channel_id, topic),
                    )
        logger.debug(f"Добавлено {len(topics)} вечнозелёных тем для {channel_id}")

    def get_evergreen_topic(self, channel_id: str) -> str | None:
        """
        Берёт наименее использованную вечнозелёную тему для канала.
        Обновляет счётчик использования.
        """
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT id, topic FROM evergreen_topics
                WHERE channel_id = ?
                ORDER BY use_count ASC, last_used_at ASC NULLS FIRST
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()

            if row is None:
                return None

            # Обновляем статистику использования
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE evergreen_topics SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                (now, row["id"]),
            )

        return row["topic"]


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
buffer = BufferManager()


# ============================================================
# ТЕСТ — запускается напрямую: python buffer_manager.py
# ============================================================
if __name__ == "__main__":
    import asyncio
    import json
    from pathlib import Path
    from ai_client import generate_post, load_channel

    async def test():
        print("📦 Тест буфера постов\n")

        # Инициализируем БД
        db.init()

        # Загружаем тестовый канал
        channel = load_channel("channels/example_channel.json")
        channel_id = channel["channel_id"]

        # Регистрируем канал в БД если нет
        with db.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM channels WHERE tg_handle = ?", (channel_id,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO channels (tg_handle, name, topic, tone, config_json) VALUES (?, ?, ?, ?, ?)",
                    (channel_id, channel["name"], channel["topic"], channel["tone"],
                     json.dumps(channel, ensure_ascii=False)),
                )
                print(f"✅ Канал зарегистрирован: {channel_id}\n")

        # Добавляем вечнозелёные темы
        buffer.add_evergreen_topics(channel_id, channel.get("evergreen_topics", []))

        # Проверяем начальный уровень буфера
        level = buffer.get_level(channel_id)
        status = buffer.check_status(channel_id)
        print(f"📊 Уровень буфера [{channel_id}]: {level} постов → статус: {status}")

        # Генерируем 3 поста и кладём в буфер
        print("\n⏳ Генерирую 3 поста и добавляю в буфер...")
        topics = [
            "ЦБ сохранил ключевую ставку 21%",
            "Как накопить на квартиру за 5 лет",
            "Инфляция в России снижается второй месяц подряд",
        ]
        for topic in topics:
            post = await generate_post(channel, topic)
            post_id = buffer.add(post)
            print(f"   ✅ Добавлен [{post['format']}] → {post_id[:8]}...")

        # Проверяем уровень после добавления
        level = buffer.get_level(channel_id)
        status = buffer.check_status(channel_id)
        print(f"\n📊 Уровень буфера после добавления: {level} постов → статус: {status}")
        print(f"   Нужна генерация? {'ДА' if buffer.needs_generation(channel_id) else 'НЕТ'}")

        # Берём следующий пост из буфера
        print("\n📤 Берём следующий пост из буфера:")
        next_post = buffer.get_next(channel_id)
        if next_post:
            print(f"   ID: {next_post['id'][:8]}...")
            print(f"   Формат: {next_post['format']}")
            print(f"   Тема: {next_post['topic']}")
            print(f"   Текст (первые 100 символов): {next_post['content'][:100]}...")

            # Имитируем публикацию
            buffer.mark_published(next_post["id"])
            print(f"   ✅ Пост помечен как опубликованный")

        # Финальный уровень
        level = buffer.get_level(channel_id)
        print(f"\n📊 Финальный уровень буфера: {level} постов")

        # Вечнозелёная тема
        eg = buffer.get_evergreen_topic(channel_id)
        print(f"\n🌿 Вечнозелёная тема для резерва: {eg}")

        print("\n✅ Буфер работает корректно!")

    asyncio.run(test())
