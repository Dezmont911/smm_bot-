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
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

from config import cfg


# Единый async-клиент на весь процесс (создаётся один раз при импорте)
aclient = anthropic.AsyncAnthropic(api_key=cfg.ANTHROPIC_API_KEY) if cfg.ANTHROPIC_API_KEY else None
openai_client = (
    AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)
    if AsyncOpenAI is not None and cfg.OPENAI_API_KEY
    else None
)


def _llm_provider() -> str:
    provider = (cfg.LLM_PROVIDER or "anthropic").strip().lower()
    return provider if provider in {"anthropic", "openai"} else "anthropic"


def _openai_model_for(requested: str | None) -> str:
    model = (requested or "").strip()
    if not model or model.startswith("claude-"):
        return cfg.OPENAI_MODEL
    return model


def _anthropic_model_for(requested: str | None) -> str:
    model = (requested or "").strip()
    if not model or model.startswith("gpt-") or model.startswith("o"):
        return cfg.CLAUDE_MODEL
    return model


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


def _extract_openai_text(response) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return str(text).strip()

    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        for block in getattr(item, "content", []) or []:
            block_text = getattr(block, "text", None)
            if block_text:
                parts.append(str(block_text))
    return "\n".join(parts).strip()


async def _openai_text(
    *,
    messages: list[dict],
    max_tokens: int,
    system: str | None,
    model: str | None,
    temperature: float | None,
    retries: int,
    purpose: str,
) -> str:
    if not cfg.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    if openai_client is None:
        raise RuntimeError("OpenAI SDK is not installed; deploy/install requirements first")

    selected_model = _openai_model_for(model)
    output_tokens = max(max_tokens, 64)
    kwargs: dict = {
        "model": selected_model,
        "input": messages,
        "max_output_tokens": output_tokens,
    }
    if selected_model.startswith("gpt-5") or selected_model.startswith("o"):
        kwargs["reasoning"] = {"effort": "minimal"}
    if system is not None:
        kwargs["instructions"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = await openai_client.responses.create(**kwargs)
            try:
                from cost_tracker import record_openai
                u = getattr(response, "usage", None)
                record_openai(
                    selected_model,
                    getattr(u, "input_tokens", 0) or 0,
                    getattr(u, "output_tokens", 0) or 0,
                    purpose=purpose,
                )
            except Exception:
                pass
            return _extract_openai_text(response)
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 2 ** attempt
                logger.warning(
                    f"OpenAI API ошибка (попытка {attempt + 1}/{retries + 1}): "
                    f"{type(e).__name__}: {e}. Повтор через {wait}с"
                )
                await asyncio.sleep(wait)

    logger.error(f"OpenAI API: все попытки исчерпаны: {last_err}")
    raise last_err


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
    provider = _llm_provider()
    if provider == "openai":
        return await _openai_text(
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            model=model,
            temperature=temperature,
            retries=retries,
            purpose=purpose,
        )

    if aclient is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    model = _anthropic_model_for(model)
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
