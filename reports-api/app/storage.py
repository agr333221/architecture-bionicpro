"""Хранение готовых отчетов в S3 (Minio) и выдача ссылок через CDN (nginx).

Структура ключей:
    reports/<user_key>/<data_version>/<date_from>_<date_to>.json

- user_key     - HMAC-SHA256(secret, email)[:32]: непредсказуемый префикс пользователя,
                 по нему нельзя подобрать чужой отчет, а все отчеты пользователя лежат рядом.
- data_version - watermark ETL (processed_until). После нового прогона ETL меняется версия,
                 а значит и URL. Поэтому кеш CDN никогда не отдает устаревший отчет
                 и его не нужно явно инвалидировать (cache busting через версию в пути).
"""
import hashlib
import hmac
import json
from datetime import date

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .config import settings

_s3 = boto3.client(
    "s3",
    endpoint_url=settings.s3_endpoint,
    aws_access_key_id=settings.s3_access_key,
    aws_secret_access_key=settings.s3_secret_key,
    region_name=settings.s3_region,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)


def user_key(email: str) -> str:
    return hmac.new(settings.user_key_secret, email.encode(), hashlib.sha256).hexdigest()[:32]


def object_key(email: str, data_version: str, date_from: date, date_to: date) -> str:
    return f"reports/{user_key(email)}/{data_version}/{date_from.isoformat()}_{date_to.isoformat()}.json"


def exists(key: str) -> bool:
    try:
        _s3.head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def put_report(key: str, report: dict) -> None:
    body = json.dumps(report, ensure_ascii=False, default=str, indent=2).encode("utf-8")
    _s3.put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8",
        # Объект неизменяем (версия в пути) -> CDN и браузер могут кешировать его
        CacheControl="private, max-age=600, immutable",
    )


def cdn_url(key: str) -> str:
    """Подписанная (SigV4) ссылка на объект, выдаваемая через хост CDN.

    Подпись считается для хоста Minio, CDN проксирует запрос в Minio
    с тем же заголовком Host, поэтому подпись остается валидной.
    """
    url = _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=settings.link_ttl,
    )
    return settings.cdn_public_url + url[len(settings.s3_endpoint.rstrip("/")):]
