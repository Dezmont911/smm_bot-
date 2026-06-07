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
    # Поддержка нескольких adminов: ADMIN_CHAT_ID=111,222,333
    ADMIN_CHAT_IDS: list = field(
        default_factory=lambda: [
            int(x.strip()) for x in _require("ADMIN_CHAT_ID").split(",")
            if x.strip().lstrip("-").isdigit()
        ]
    )

    @property
    def ADMIN_CHAT_ID(self) -> int:
        """Первый (главный) admin ID — для обратной совместимости."""
        return self.ADMIN_CHAT_IDS[0] if self.ADMIN_CHAT_IDS else 0

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
    # Целевой уровень добора: при буфере ниже него докручиваем очередь ДО него
    # (и генерация, и референсы добивают именно до BUFFER_TARGET, не переполняя).
    BUFFER_TARGET: int = field(
        default_factory=lambda: int(_optional("BUFFER_TARGET", "5"))
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

    # --- WB Seller API ---
    # Ключ из seller.wildberries.ru → Настройки → Интеграции по API → Создать токен
    # Нужные права: ✅ Контент, ✅ Цены и скидки
    WB_API_KEY: str = field(
        default_factory=lambda: _optional("WB_API_KEY", "")
    )
    # Режим WB парсера: "seller" (свои товары) | "search" (поиск по категориям) | "auto"
    WB_API_MODE: str = field(
        default_factory=lambda: _optional("WB_API_MODE", "auto")
    )
    # Прокси для WB парсера (резидентный — обходит блокировку datacenter IP)
    # Один прокси: http://user:pass@host:port
    WB_PROXY_URL: str = field(
        default_factory=lambda: _optional("WB_PROXY_URL", "")
    )
    # Несколько прокси через запятую — парсер ротирует их случайно
    # Пример: http://u1:p@1.2.3.4:80,http://u2:p@5.6.7.8:80
    WB_PROXY_URLS: list = field(
        default_factory=lambda: [
            u.strip() for u in _optional("WB_PROXY_URLS", "").split(",")
            if u.strip()
        ]
    )

    # --- fal.ai (генерация картинок через FLUX) ---
    FAL_API_KEY: str = field(
        default_factory=lambda: _optional("FAL_API_KEY", "")
    )

    # --- TwiBoost / post boost ---
    TWIBOOST_API_KEY: str = field(
        default_factory=lambda: _optional("TWIBOOST_API_KEY", "")
    )
    TWIBOOST_API_URL: str = field(
        default_factory=lambda: _optional("TWIBOOST_API_URL", "https://twiboost.com/api/v2")
    )
    TWIBOOST_VIEWS_SERVICE_ID: int = field(
        default_factory=lambda: int(v) if (v := _optional("TWIBOOST_VIEWS_SERVICE_ID", "0").strip()).isdigit() else 0
    )
    BOOST_DEFAULT_QUANTITY: int = field(
        default_factory=lambda: int(v) if (v := _optional("BOOST_DEFAULT_QUANTITY", "500").strip()).isdigit() else 500
    )
    BOOST_DRY_RUN: bool = field(
        default_factory=lambda: _optional("BOOST_DRY_RUN", "true").lower() == "true"
    )
    BOOST_REAL_ORDERS_ENABLED: bool = field(
        default_factory=lambda: _optional("BOOST_REAL_ORDERS_ENABLED", "false").lower() == "true"
    )

    # --- Учёт расходов на сервисы (цены в USD, можно переопределить в .env) ---
    # Claude — цена за 1 млн токенов (вход/выход). По умолчанию — Haiku 4.5 ($1/$5).
    # Для sonnet/opus цена выбирается автоматически по имени модели (см. cost_tracker).
    CLAUDE_INPUT_USD_PER_MTOK: float = field(
        default_factory=lambda: float(_optional("CLAUDE_INPUT_USD_PER_MTOK", "1.0"))
    )
    CLAUDE_OUTPUT_USD_PER_MTOK: float = field(
        default_factory=lambda: float(_optional("CLAUDE_OUTPUT_USD_PER_MTOK", "5.0"))
    )
    # fal.ai FLUX schnell — цена за одну картинку.
    FAL_IMAGE_USD: float = field(
        default_factory=lambda: float(_optional("FAL_IMAGE_USD", "0.003"))
    )

    # --- Кэш тем из веб-поиска ---
    # За один поиск берём с запасом, лишнее кладём в кэш и переиспользуем,
    # пока не протухнет (TTL). Срезает число обращений к веб-поиску.
    TOPIC_CACHE_TTL_HOURS: int = field(
        default_factory=lambda: int(_optional("TOPIC_CACHE_TTL_HOURS", "8"))
    )
    # Сколько тем запрашивать за один поиск (с запасом сверх нужного)
    TOPIC_SEARCH_BATCH: int = field(
        default_factory=lambda: int(_optional("TOPIC_SEARCH_BATCH", "15"))
    )

    # --- Семантический дедуп ---
    # Порог cosine-близости: посты выше него считаются смысловыми дублями.
    # 0.85 — ловит перефраз, но не режет реально разные посты (проверено на модели).
    DEDUP_THRESHOLD: float = field(
        default_factory=lambda: float(_optional("DEDUP_THRESHOLD", "0.85"))
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
