"""reports-api: отчеты о работе протеза.

GET /reports?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD[&user=<email>]

1. Проверяет JWT (подпись, issuer, audience) и роль prothetic_user.
2. Пользователь может получить отчет только о себе: email берется из токена,
   параметр user (если передан) обязан совпадать с ним, иначе 403.
3. Период ограничивается данными, которые уже обработал ETL (watermark Airflow).
4. Если отчет за этот период и эту версию данных уже лежит в S3 - сразу отдаем ссылку на CDN,
   OLAP-база не запрашивается. Иначе строим отчет из витрины ClickHouse, кладем в S3
   и отдаем ссылку на CDN.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query

from . import olap, storage
from .auth import Principal, get_principal
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("reports-api")

app = FastAPI(title="bionicpro-reports-api", version="1.0.0")


def _summary(rows: list[dict]) -> list[dict]:
    """Итоги по каждому протезу за период (простое суммирование готовых дневных агрегатов)."""
    by_serial: dict[str, dict] = {}
    for r in rows:
        s = by_serial.setdefault(r["prosthesis_serial"], {
            "prosthesis_serial": r["prosthesis_serial"],
            "prosthesis_model": r["prosthesis_model"],
            "days": 0, "events_count": 0, "movements_count": 0, "errors_count": 0,
            "slow_reactions_count": 0, "max_reaction_ms": 0, "min_battery_level": 100,
            "_reaction_weighted": 0.0,
        })
        s["days"] += 1
        s["events_count"] += int(r["events_count"])
        s["movements_count"] += int(r["movements_count"])
        s["errors_count"] += int(r["errors_count"])
        s["slow_reactions_count"] += int(r["slow_reactions_count"])
        s["max_reaction_ms"] = max(s["max_reaction_ms"], int(r["max_reaction_ms"]))
        s["min_battery_level"] = min(s["min_battery_level"], int(r["min_battery_level"]))
        s["_reaction_weighted"] += float(r["avg_reaction_ms"]) * int(r["events_count"])
    result = []
    for s in by_serial.values():
        events = s["events_count"] or 1
        s["avg_reaction_ms"] = round(s.pop("_reaction_weighted") / events, 1)
        s["slow_reactions_share"] = round(s["slow_reactions_count"] / events, 4)
        result.append(s)
    return result


@app.get("/reports")
def get_report(
    principal: Principal = Depends(get_principal),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    user: str | None = Query(default=None, description="email; допускается только свой"),
) -> dict:
    # --- Доступ только к собственному отчету ---
    if user is not None and user.lower() != principal.email:
        log.warning("user %s tried to read report of %s", principal.email, user)
        raise HTTPException(403, "you can request only your own report")

    # --- Только период, уже обработанный ETL ---
    watermark = olap.processed_until()
    if watermark is None:
        raise HTTPException(503, "reports are not ready yet: ETL has not processed any data")
    # Полностью обработанным считаем последний день, закончившийся до watermark
    # (текущий неполный день в отчет не попадает)
    available_until = watermark.date() - timedelta(days=1)

    date_to = date_to or available_until
    date_from = date_from or (date_to - timedelta(days=settings.default_period_days - 1))
    if date_from > date_to:
        raise HTTPException(400, "date_from must be <= date_to")
    if date_from > available_until:
        raise HTTPException(409, {
            "message": "data for the requested period has not been processed yet",
            "available_until": available_until.isoformat(),
        })
    truncated = date_to > available_until
    date_to = min(date_to, available_until)
    if (date_to - date_from).days + 1 > settings.max_period_days:
        raise HTTPException(400, f"period is longer than {settings.max_period_days} days")

    data_version = watermark.strftime("%Y%m%dT%H%M%SZ")
    key = storage.object_key(principal.email, data_version, date_from, date_to)

    # --- Кеш в S3: повторный запрос не нагружает OLAP ---
    if storage.exists(key):
        cache = "hit"
    else:
        cache = "miss"
        customer, rows = olap.load_report_rows(principal.email, date_from, date_to)
        if customer is None:
            raise HTTPException(404, "no prostheses registered in CRM for this user")
        report = {
            "customer": customer,
            "period": {"from": date_from, "to": date_to},
            "data_version": data_version,
            "source": settings.report_source,
            "generated_at": datetime.now(timezone.utc),
            "summary": _summary(rows),
            "daily": rows,
        }
        storage.put_report(key, report)
        log.info("report generated: user=%s period=%s..%s rows=%d", principal.email, date_from, date_to, len(rows))

    return {
        "user": principal.email,
        "period": {"from": date_from, "to": date_to},
        "available_until": available_until,
        "truncated": truncated,
        "data_version": data_version,
        "cache": cache,
        "url": storage.cdn_url(key),
        "url_expires_in": settings.link_ttl,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
