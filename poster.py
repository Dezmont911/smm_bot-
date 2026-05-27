"""
poster.py — Планировщик публикаций (Слой 4 из handbook)

Этот модуль делает одну вещь: берёт готовые посты из буфера
и публикует их в Telegram-каналы по расписанию.

Логика работы:
  1. Каждый час (или по расписанию канала) — проверяем нужно ли постить
  2. Берём следующий ready пост из буфера
  3. Публикуем в канал
  4. Помечаем как published
  5. Проверяем уровень буфера — если мало, запускаем генерацию
  6. Если буфер пустой — алерт администратору

Расписание по умолчанию: 4 поста в день (09:00, 12:00, 16:00, 20:00 МСК)
Можно задать своё расписание в карточке канала (поле post_times).

Запуск встроен в bot.py через APScheduler — отдельно запускать не нужно.
"""

from datetime import datetime, timezone
from loguru import logger

from buffer_manager import buffer
from config import cfg
from database import db


class Poster:
    """Публикует посты из буфера в Telegram-каналы."""

    # Расписание по умолчанию (часы по МСК = UTC+3)
    # Переводим в UTC: 09:00 МСК = 06:00 UTC и т.д.
    DEFAULT_POST_HOURS_UTC = [6, 9, 13, 17]  # 09, 12, 16, 20 МСК

    def __init__(self):
        self.bot = None  # устанавливается при старте через set_bot()

    def set_bot(self, bot):
        """Привязывает Telegram бот для публикации и алертов."""
        self.bot = bot

    # --------------------------------------------------------
    # Главный метод — вызывается планировщиком каждый час
    # --------------------------------------------------------

    async def tick(self):
        """
        Основной цикл постера — вызывается каждый час.
        Проверяет каждый канал: нужно ли сейчас постить?
        """
        if self.bot is None:
            logger.warning("Постер не инициализирован (bot=None), пропускаю")
            return

        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.hour

        channels = self._load_active_channels()
        if not channels:
            return

        logger.debug(f"Постер: проверяю {len(channels)} каналов | час UTC: {current_hour}")

        for channel in channels:
            try:
                await self._process_channel(channel, current_hour)
            except Exception as e:
                logger.error(f"Ошибка постера для {channel['channel_id']}: {e}")

    async def _process_channel(self, channel: dict, current_hour: int):
        """
        Обрабатывает один канал: постит если пришло время,
        проверяет буфер и при необходимости запускает генерацию.
        """
        channel_id = channel["channel_id"]

        # Проверяем: пришло ли время для этого канала?
        post_hours = channel.get("post_times_utc", self.DEFAULT_POST_HOURS_UTC)
        if current_hour not in post_hours:
            return  # не время — пропускаем

        # Берём следующий пост из буфера
        post = buffer.get_next(channel_id)

        if post is None:
            # Буфер пуст — алерт администратору
            logger.warning(f"⚠️ Буфер пуст! Канал: {channel_id}")
            await self._alert_empty_buffer(channel_id)
            return

        # Публикуем пост
        success = await self._publish(post)

        if success:
            buffer.mark_published(post["id"])
            logger.success(
                f"Опубликован пост в {channel_id} | "
                f"формат: {post.get('format', '?')} | "
                f"id: {post['id'][:8]}..."
            )

            # Проверяем буфер после публикации
            await self._check_buffer_after_post(channel)
        else:
            logger.error(f"Не удалось опубликовать пост в {channel_id}")
            await self._alert(
                f"❌ <b>Ошибка публикации</b>\n"
                f"Канал: {channel_id}\n"
                f"Пост: {post['id'][:8]}...\n"
                f"Проверь логи."
            )

    # --------------------------------------------------------
    # Публикация поста
    # --------------------------------------------------------

    async def _publish(self, post: dict) -> bool:
        """
        Отправляет пост в Telegram-канал.
        Порядок попыток:
          1. Markdown + картинка
          2. Plain text + картинка
          3. Markdown без картинки  (если картинка недоступна)
          4. Plain text без картинки
        Возвращает True если хотя бы одна попытка успешна.
        """
        channel_id = post["channel_id"]
        content = post["content"]
        image_url = post.get("image_url")

        # Формируем список попыток: (parse_mode, use_image)
        if image_url:
            attempts = [
                ("Markdown", True),
                (None,       True),
                ("Markdown", False),  # картинка недоступна — пробуем без неё
                (None,       False),
            ]
        else:
            attempts = [
                ("Markdown", False),
                (None,       False),
            ]

        for parse_mode, use_image in attempts:
            img = image_url if use_image else None
            try:
                if img:
                    await self.bot.send_photo(
                        chat_id=channel_id,
                        photo=img,
                        caption=content,
                        parse_mode=parse_mode,
                    )
                else:
                    await self.bot.send_message(
                        chat_id=channel_id,
                        text=content,
                        parse_mode=parse_mode,
                    )
                if image_url and not use_image:
                    logger.warning(f"Пост опубликован БЕЗ картинки (недоступна): {channel_id}")
                return True

            except Exception as e:
                logger.warning(
                    f"Попытка [parse={parse_mode}, img={'да' if use_image else 'нет'}] "
                    f"в {channel_id} не удалась: {e}"
                )
                continue  # переходим к следующей попытке

        logger.error(f"Все попытки публикации в {channel_id} провалились")
        return False

    # --------------------------------------------------------
    # Проверка буфера и запуск генерации
    # --------------------------------------------------------

    async def _check_buffer_after_post(self, channel: dict):
        """
        После каждой публикации проверяем уровень буфера.
        Если постов мало — запускаем фоновую генерацию.
        """
        channel_id = channel["channel_id"]
        level = buffer.get_level(channel_id)
        status = buffer.check_status(channel_id)

        if status == "critical":
            # Буфер критически мал — алерт + срочная генерация
            logger.warning(f"🚨 Критический уровень буфера [{channel_id}]: {level} постов")
            await self._alert(
                f"🚨 <b>Критически мало постов!</b>\n"
                f"Канал: {channel_id}\n"
                f"Осталось: {level} постов\n"
                f"Запускаю экстренную генерацию..."
            )
            await self._run_emergency_generation(channel_id)

        elif status == "emergency":
            # Мало — тихо запускаем генерацию в фоне
            logger.info(f"⚡ Мало постов [{channel_id}]: {level} — запускаю генерацию")
            import asyncio
            asyncio.create_task(self._run_emergency_generation(channel_id))

    async def _run_emergency_generation(self, channel_id: str):
        """Запускает экстренную генерацию для канала."""
        try:
            from content_generator import generator
            result = await generator.run_emergency(channel_id)
            generated = result.get("generated", 0)

            if generated > 0:
                logger.success(f"Экстренная генерация [{channel_id}]: +{generated} постов")
                await self._alert(
                    f"✅ <b>Экстренная генерация завершена</b>\n"
                    f"Канал: {channel_id}\n"
                    f"Добавлено постов: {generated}"
                )
            else:
                logger.error(f"Экстренная генерация [{channel_id}]: 0 постов!")
                await self._alert(
                    f"❌ <b>Экстренная генерация не дала постов!</b>\n"
                    f"Канал: {channel_id}\n"
                    f"Проверь RSS-источники и вечнозелёные темы."
                )
        except Exception as e:
            logger.error(f"Ошибка экстренной генерации [{channel_id}]: {e}")

    # --------------------------------------------------------
    # Алерты администратору
    # --------------------------------------------------------

    async def _alert(self, text: str):
        """Отправляет сообщение администратору."""
        try:
            await self.bot.send_message(
                chat_id=cfg.ADMIN_CHAT_ID,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось отправить алерт: {e}")

    async def _alert_empty_buffer(self, channel_id: str):
        """Алерт о пустом буфере + попытка экстренной генерации."""
        await self._alert(
            f"📭 <b>Буфер пуст!</b>\n"
            f"Канал: {channel_id}\n"
            f"Пост не был опубликован.\n"
            f"Запускаю генерацию..."
        )
        await self._run_emergency_generation(channel_id)

    # --------------------------------------------------------
    # Вспомогательные
    # --------------------------------------------------------

    def _load_active_channels(self) -> list[dict]:
        """Загружает все активные карточки каналов из БД."""
        import json
        from pathlib import Path

        channels_dir = Path(__file__).parent / "channels"
        channels = []
        for json_file in channels_dir.glob("*.json"):
            if json_file.name.startswith("example_"):
                continue
            try:
                with open(json_file, encoding="utf-8") as f:
                    ch = json.load(f)
                if ch.get("active", True):
                    channels.append(ch)
            except Exception as e:
                logger.error(f"Ошибка загрузки канала {json_file.name}: {e}")
        return channels

    async def post_now(self, channel_id: str) -> dict:
        """
        Публикует следующий пост немедленно (для /post_now команды).
        Возвращает результат: {'success': True, 'post': {...}}
        """
        post = buffer.get_next(channel_id)
        if post is None:
            return {"success": False, "error": "Буфер пуст"}

        success = await self._publish(post)
        if success:
            buffer.mark_published(post["id"])
            return {"success": True, "post": post}
        else:
            return {"success": False, "error": "Ошибка отправки в Telegram"}


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
poster = Poster()
