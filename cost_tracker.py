"""
cost_tracker.py — учёт фактических расходов на платные сервисы (Claude, fal.ai).

Зачем: посмотреть «сколько потрачено» за период (сегодня / 7 / 30 дней / всё время /
произвольно). Баланс/остаток сервисы публично не отдают, поэтому считаем САМИ по
факту каждого вызова: токены Claude × цена + картинки FLUX × цена.

Запись идёт из единых точек:
  - claude_helper.claude_text → record_claude(...)   (на каждый ответ модели)
  - image_generator.generate_image → record_fal(...) (на каждую готовую картинку)

Запись «мягкая»: любая ошибка учёта НЕ должна ломать генерацию/постинг (всё в try).
"""

from datetime import datetime, timedelta, timezone

from loguru import logger

from config import cfg
from database import db


# Цена за 1 млн токенов (input, output), USD — по семейству модели.
# Если модель не распознана — берём дефолт из .env (CLAUDE_*_USD_PER_MTOK).
_PRICING = {
    "haiku":  (1.0, 5.0),
    "sonnet": (3.0, 15.0),
    "opus":   (15.0, 75.0),
}

# Часовой пояс для границы «сегодня» (МСК = UTC+3) — чтобы «сегодня» совпадало
# с календарным днём Arthur, а не с UTC-полночью.
_MSK_OFFSET_H = 3


def _utcnow_str() -> str:
    """Текущее время UTC в фиксированном формате (для лексикограф. сравнения ts)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _claude_price(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in _PRICING.items():
        if key in m:
            return price
    return (cfg.CLAUDE_INPUT_USD_PER_MTOK, cfg.CLAUDE_OUTPUT_USD_PER_MTOK)


def _insert(service: str, model: str, purpose: str,
            in_tok: int, out_tok: int, units: int, cost: float) -> None:
    try:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO usage_costs "
                "(ts, service, model, purpose, input_tokens, output_tokens, units, cost_usd) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_utcnow_str(), service, model or "", purpose or "",
                 int(in_tok or 0), int(out_tok or 0), int(units or 0), float(cost or 0.0)),
            )
    except Exception as e:
        logger.debug(f"cost_tracker: не записал расход ({service}): {e}")


def record_claude(model: str, input_tokens: int, output_tokens: int, purpose: str = "") -> None:
    """Учитывает стоимость одного ответа Claude по токенам."""
    pin, pout = _claude_price(model)
    cost = (input_tokens or 0) / 1_000_000 * pin + (output_tokens or 0) / 1_000_000 * pout
    _insert("claude", model, purpose, input_tokens, output_tokens, 0, cost)


def record_fal(units: int = 1, model: str = "fal-ai/flux/schnell", purpose: str = "image") -> None:
    """Учитывает стоимость сгенерированных картинок FLUX."""
    cost = (units or 0) * cfg.FAL_IMAGE_USD
    _insert("fal", model, purpose, 0, 0, units, cost)


# ── Запросы по периодам ─────────────────────────────────────────────────────

def since_today_msk() -> str:
    """ts-граница начала сегодняшнего дня по МСК (в формате UTC ts)."""
    now = datetime.now(timezone.utc)
    msk = now + timedelta(hours=_MSK_OFFSET_H)
    msk_midnight = msk.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_midnight = msk_midnight - timedelta(hours=_MSK_OFFSET_H)
    return utc_midnight.strftime("%Y-%m-%dT%H:%M:%S")


def since_days(n: int) -> str:
    """ts-граница «n дней назад от текущего момента»."""
    dt = datetime.now(timezone.utc) - timedelta(days=max(0, n))
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def summary(since: str | None = None) -> dict:
    """
    Сводка расходов за период (since = ts-граница; None = всё время).

    Возвращает:
      {"claude": {"cost","calls","in_tok","out_tok"},
       "fal":    {"cost","calls","units"},
       "total":  float}
    """
    out = {
        "claude": {"cost": 0.0, "calls": 0, "in_tok": 0, "out_tok": 0},
        "fal":    {"cost": 0.0, "calls": 0, "units": 0},
        "total":  0.0,
    }
    try:
        where = "WHERE ts >= ?" if since else ""
        params = (since,) if since else ()
        with db.connect() as conn:
            rows = conn.execute(
                f"""SELECT service,
                           COUNT(*)                      AS calls,
                           COALESCE(SUM(input_tokens),0) AS in_tok,
                           COALESCE(SUM(output_tokens),0) AS out_tok,
                           COALESCE(SUM(units),0)        AS units,
                           COALESCE(SUM(cost_usd),0)     AS cost
                    FROM usage_costs {where} GROUP BY service""",
                params,
            ).fetchall()
        for r in rows:
            svc = r["service"]
            if svc not in out:
                continue
            out[svc]["cost"] = float(r["cost"] or 0.0)
            out[svc]["calls"] = int(r["calls"] or 0)
            if svc == "claude":
                out["claude"]["in_tok"] = int(r["in_tok"] or 0)
                out["claude"]["out_tok"] = int(r["out_tok"] or 0)
            else:
                out["fal"]["units"] = int(r["units"] or 0)
            out["total"] += float(r["cost"] or 0.0)
    except Exception as e:
        logger.debug(f"cost_tracker.summary: {e}")
    return out
