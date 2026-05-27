"""
config.py — Конфигурация проекта

Загружает все переменные из .env файла и предоставляет
их остальным модулям как удобные константы.

Использование в других файлах:
    from config import cfg
    print(cfg.BOT_TOKEN)
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

# Загружаем .env файл (ищет его в текущей папке или выше)
load_dotenv()


def _require(key: str) -> str:
    """Берёт переменную из окружения. Падает с понятной ошибкой, если её нет."""
    value = os.getenv(key)
    if not value:
        raise ValueError(
            f"\n❌ Переменная '{key}' не найдена в .env файле!\n"
            f"   Скопируй .env.example → .env и заполни значение."
        )
    return value


def _optional(key: str, default: str = "") -> str:
    """Берёт переменную из окружения. Возвращает default, если её нет."""
    return os.getenv(key, default)


@dataclass
class Config:
    """Все настройки проекта в одном месте."""

    # --- Telegram Bot ---
    BOT_TOKEN: str = field(default_factory=lambda: _require("BOT_TOKEN"))
    ADMIN_CHAT_ID: int = field(
        default_factory=lambda: int(_require("ADMIN_CHAT_ID"))
    )

    # --- Telegram User API (Telethon) ---
    # Необязательные пока — нужны только для Слоя 4 (мониторинг рекламы)
    TELEGRAM_API_ID: int = field(
        default_factory=lambda: int(v) if (v := _optional("TELEGRAM_API_ID", "0")).isdigit() else 0
    )
    TELEGRAM_API_HASH: str = field(
        default_factory=lambda: _optional("TELEGRAM_API_HASH", "")
    )
    TELEGRAM_PHONE: str = field(
        default_factory=lambda: _optional("TELEGRAM_PHONE", "")
    )

    # --- Картинки для постов ---
    UNSPLASH_ACCESS_KEY: str = field(
        default_factory=lambda: _optional("UNSPLASH_ACCESS_KEY", "")
    )
    PEXELS_API_KEY: str = field(
        default_factory=lambda: _optional("PEXELS_API_KEY", "")
    )

    # --- Claude API ---
    ANTHROPIC_API_KEY: str = field(
        default_factory=lambda: _require("ANTHROPIC_API_KEY")
    )
    CLAUDE_MODEL: str = field(
        default_factory=lambda: _optional("CLAUDE_MODEL", "claude-sonnet-4-5")
    )

    # --- База данных ---
    # Необязательная пока — нужна для Слоя 3 (буфер постов)
    DATABASE_URL: str = field(
        default_factory=lambda: _optional("DATABASE_URL", "")
    )

    # --- Redis ---
    REDIS_URL: str = field(
        default_factory=lambda: _optional("REDIS_URL", "redis://localhost:6379/0")
    )

    # --- Настройки буфера ---
    BUFFER_MIN: int = field(
        default_factory=lambda: int(_optional("BUFFER_MIN", "8"))
    )
    BUFFER_EMERGENCY: int = field(
        default_factory=lambda: int(_optional("BUFFER_EMERGENCY", "4"))
    )
    BUFFER_CRITICAL: int = field(
        default_factory=lambda: int(_optional("BUFFER_CRITICAL", "2"))
    )

    # --- Задержка публикации (в секундах) ---
    POST_DELAY_MIN: int = field(
        default_factory=lambda: int(_optional("POST_DELAY_MIN", "300"))
    )
    POST_DELAY_MAX: int = field(
        default_factory=lambda: int(_optional("POST_DELAY_MAX", "900"))
    )

    # --- Планировщик ---
    GENERATION_HOUR: int = field(
        default_factory=lambda: int(_optional("GENERATION_HOUR", "3"))
    )
    GENERATION_MINUTE: int = field(
        default_factory=lambda: int(_optional("GENERATION_MINUTE", "0"))
    )

    # --- Мониторинг ---
    MONITOR_INTERVAL: int = field(
        default_factory=lambda: int(_optional("MONITOR_INTERVAL", "180"))
    )

    # --- Режим отладки ---
    DEBUG: bool = field(
        default_factory=lambda: _optional("DEBUG", "True").lower() == "true"
    )


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР — импортируй его во всех файлах:
#   from config import cfg
# ============================================================
cfg = Config()


# --- Быстрая проверка при запуске ---
if __name__ == "__main__":
    print("✅ Конфигурация загружена успешно!")
    print(f"   Claude модель:    {cfg.CLAUDE_MODEL}")
    print(f"   Буфер (мин):      {cfg.BUFFER_MIN} постов")
    print(f"   Буфер (критично): {cfg.BUFFER_CRITICAL} постов")
    print(f"   Задержка поста:   {cfg.POST_DELAY_MIN}–{cfg.POST_DELAY_MAX} сек")
    print(f"   Генерация в:      {cfg.GENERATION_HOUR:02d}:{cfg.GENERATION_MINUTE:02d} UTC")
    print(f"   Debug режим:      {cfg.DEBUG}")
