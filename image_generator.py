"""
image_generator.py — Генерация изображений через fal.ai (FLUX.1 Schnell)

Используется для кнопки "🎨 Сгенерировать изображение" в боте.
FLUX.1 Schnell — самая быстрая модель, ~1-3 секунды, ~$0.003/картинка.

Требования:
    pip install fal-client

Переменная окружения:
    FAL_API_KEY=ваш_ключ
"""

import asyncio
import re
from loguru import logger
from config import cfg
from claude_helper import claude_text


async def generate_image(
    topic: str,
    channel_topic: str = "",
    channel_name: str = "",
) -> str | None:
    """
    Генерирует изображение через fal.ai FLUX.1 Schnell.

    Логика:
      1. Claude Haiku строит визуальный промпт на английском по теме поста
      2. FLUX генерирует картинку (1024×576 — оптимально для Telegram)
      3. Возвращает прямой URL картинки

    Args:
        topic         — тема поста (может быть на русском)
        channel_topic — тематика канала для контекста
        channel_name  — название канала

    Returns:
        URL сгенерированной картинки или None при ошибке
    """
    if not cfg.FAL_API_KEY:
        logger.warning("FAL_API_KEY не задан — генерация недоступна")
        return None

    # Шаг 1: строим промпт через Claude
    prompt = await _build_image_prompt(topic, channel_topic, channel_name)
    if not prompt:
        logger.warning("Не удалось построить промпт для генерации")
        return None

    logger.info(f"Картинка (FLUX): пост «{(topic or '')[:60]}…» → промпт '{prompt[:80]}'")

    # Шаг 2: запускаем FLUX через fal.ai
    try:
        import fal_client
        import os
        os.environ["FAL_KEY"] = cfg.FAL_API_KEY

        def _run_sync():
            result = fal_client.run(
                "fal-ai/flux/schnell",
                arguments={
                    "prompt": prompt,
                    "image_size": "landscape_16_9",   # 1024×576 — идеально для Telegram
                    "num_inference_steps": 4,          # Schnell оптимален при 4 шагах
                    "num_images": 1,
                    "enable_safety_checker": True,
                },
            )
            return result

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run_sync)

        images = result.get("images", [])
        if not images:
            logger.warning("fal.ai вернул пустой список изображений")
            return None

        url = images[0].get("url")
        if url:
            logger.info(f"fal.ai OK | {url[:80]}...")
            try:
                from cost_tracker import record_fal
                record_fal(1)
            except Exception:
                pass
        return url

    except ImportError:
        logger.error("fal-client не установлен. Выполни: pip install fal-client")
        return None
    except Exception as e:
        logger.error(f"fal.ai ошибка: {type(e).__name__}: {e}")
        return None


async def _build_image_prompt(
    topic: str,
    channel_topic: str = "",
    channel_name: str = "",
) -> str | None:
    """
    Строит детальный промпт для FLUX на основе темы поста.

    FLUX понимает английский — всегда переводим и детализируем через Claude.
    Хороший промпт: конкретные визуальные детали, стиль, освещение.
    """
    try:
        context = ""
        if channel_name:
            context += f"Channel: {channel_name}. "
        if channel_topic:
            context += f"Topic area: {channel_topic}. "

        system = (
            "You are an expert at writing image generation prompts for FLUX AI model. "
            "Create vivid, specific, visually descriptive prompts in English. "
            "Focus on visual elements: scene, style, lighting, colors, composition. "
            "Avoid abstract concepts. For esports or video game posts, depict gaming PCs, "
            "screens, tournaments, game UI mood, or player setups; never depict real-world "
            "sports fields, balls, or outdoor athletes. Keep it under 50 words."
        )

        # Передаём ВЕСЬ пост, но просим иллюстрировать его ЦЕНТРАЛЬНУЮ тему и
        # игнорировать разговорную «воду» (приветствия, эмоции, мнения) — иначе
        # эмоциональный крючок в начале уводит картинку не в тему.
        user_msg = (
            f"{context}"
            f"Below is a social-media post (may be in Russian, with conversational filler — "
            f"greetings, emotions, opinions). Identify its CENTRAL concrete subject and write "
            f"an image prompt illustrating THAT subject. Ignore the filler.\n\n"
            f"Post:\n'{topic}'\n\n"
            f"Requirements:\n"
            f"- Photorealistic or high-quality digital art style\n"
            f"- Depicts the main subject of the post (not the emotional intro)\n"
            f"- If this is about esports/video games, no real-world sports scene\n"
            f"- No text, logos, watermarks in the image\n"
            f"- Return ONLY the prompt, nothing else"
        )

        prompt = await claude_text(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )

        # Убираем кавычки если Claude обернул ответ
        prompt = re.sub(r'^["\'"]|["\'"]$', "", prompt).strip()
        return prompt if prompt else None

    except Exception as e:
        logger.warning(f"Ошибка построения промпта: {e}")
        return None


# ============================================================
# ТЕСТ — python image_generator.py
# ============================================================
if __name__ == "__main__":
    import asyncio
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    async def test():
        print("=== Тест генерации картинок ===\n")

        if not cfg.FAL_API_KEY:
            print("❌ FAL_API_KEY не задан в .env")
            return

        test_cases = [
            ("Топ-5 модов для выживания в Minecraft", "майнкрафт, игры", "РП Майнкрафт"),
            ("Новинки игровой индустрии 2024", "игры, новости", "Neffyi Channel"),
            ("Интересные факты о вселенной", "интересные факты", "TFT Fun Time"),
        ]

        for topic, ch_topic, ch_name in test_cases:
            print(f"Тема: {topic}")
            url = await generate_image(topic, ch_topic, ch_name)
            if url:
                print(f"  ✅ {url[:90]}...")
            else:
                print(f"  ❌ Ошибка генерации")
            print()

    asyncio.run(test())
