"""
claude_helper.py — единая точка вызова Anthropic Claude API.

Зачем нужен:
  - Один общий АСИНХРОННЫЙ клиент на весь процесс (anthropic.AsyncAnthropic).
    Синхронный anthropic.Anthropic внутри async-кода блокировал event loop —
    пока шла генерация поста, бот не отвечал на команды. Async-клиент это чинит.
  - Ретраи на транзиентных ошибках API (сеть, rate limit) с экспоненциальным бэкоффом.
  - Безопасное извлечение текста из ответа (без падения на пустом/нетекстовом блоке).

Использование:
    from claude_helper import claude_text
    text = await claude_text(
        max_tokens=1024,
        messages=[{"role": "user", "content": "..."}],
        system="...",          # опционально
        model="claude-...",    # опционально, по умолчанию cfg.CLAUDE_MODEL
    )
"""

import asyncio

import anthropic
from loguru import logger

from config import cfg


# Единый async-клиент на весь процесс (создаётся один раз при импорте)
aclient = anthropic.AsyncAnthropic(api_key=cfg.ANTHROPIC_API_KEY)


def _extract_text(message) -> str:
    """
    Безопасно достаёт текст из ответа Claude.
    Возвращает "" если в ответе нет текстового блока.
    """
    try:
        for block in message.content:
            if getattr(block, "type", None) == "text":
                return block.text.strip()
        # fallback — вдруг первый блок имеет .text без type
        first = message.content[0]
        return getattr(first, "text", "").strip()
    except (IndexError, AttributeError, TypeError):
        return ""


async def claude_text(
    *,
    messages: list[dict],
    max_tokens: int = 1024,
    system: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    retries: int = 2,
    purpose: str = "",
) -> str:
    """
    Вызывает Claude и возвращает текст ответа.

    temperature — управляет вариативностью (None = дефолт модели). Выше → разнообразнее.

    Делает до `retries` повторов на транзиентных ошибках API
    (APIError / APIConnectionError) с бэкоффом 1с, 2с, 4с...
    При полном провале пробрасывает последнее исключение —
    вызывающий код решает, что делать (большинство уже обёрнуто в try/except).
    """
    model = model or cfg.CLAUDE_MODEL
    kwargs: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system is not None:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            message = await aclient.messages.create(**kwargs)
            # Учёт расходов (мягко — не ломаем вызов при сбое учёта)
            try:
                from cost_tracker import record_claude
                u = getattr(message, "usage", None)
                if u is not None:
                    record_claude(
                        model,
                        getattr(u, "input_tokens", 0) or 0,
                        getattr(u, "output_tokens", 0) or 0,
                        purpose=purpose,
                    )
            except Exception:
                pass
            return _extract_text(message)
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            last_err = e
            if attempt < retries:
                wait = 2 ** attempt
                logger.warning(
                    f"Claude API ошибка (попытка {attempt + 1}/{retries + 1}): "
                    f"{type(e).__name__}: {e}. Повтор через {wait}с"
                )
                await asyncio.sleep(wait)
        except Exception as e:
            # Неизвестная ошибка (например, неверные аргументы) — не ретраим
            logger.error(f"Claude API неожиданная ошибка: {type(e).__name__}: {e}")
            raise

    logger.error(f"Claude API: все попытки исчерпаны: {last_err}")
    raise last_err
