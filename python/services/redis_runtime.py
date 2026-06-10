from __future__ import annotations

from typing import Any

import structlog

from config.settings import Settings
from services.feature_store import FeatureStore

logger = structlog.get_logger()


async def create_redis_feature_store(
    settings: Settings,
) -> tuple[Any | None, FeatureStore | None, str]:
    """Redis 可达时创建基于 Redis 的 FeatureStore。"""
    client = None
    try:
        import redis.asyncio as redis

        client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        await client.ping()
        logger.info("redis.connected", url=settings.redis_url)
        return client, FeatureStore(client, ttl=settings.feature_ttl_seconds), ""
    except Exception as exc:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        reason = str(exc)
        logger.warning("redis.unavailable", error=reason)
        return None, None, reason
