"""api: gateway in front of orders, with a Redis cache.

There is deliberately no fallback when Redis fails: a Redis outage makes the API fail,
which shows up first as errors on the frontend (the cascading-failure fault).
"""

import json
import os

import httpx
import redis.asyncio as redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from fastapi import HTTPException, Request

from shop.common import create_app, setup_logging, timed_dependency

log = setup_logging()

REDIS_HOST = os.environ["REDIS_HOST"]
ORDERS_URL = os.environ["ORDERS_URL"]
CACHE_KEY = "orders:recent"
CACHE_TTL_SECONDS = 5

# One immediate retry. redis-py's default (several retries with backoff) makes a Redis
# outage take 15s+ per request, which makes the failure timing unpredictable.
cache = redis.Redis(
    host=REDIS_HOST,
    port=6379,
    socket_timeout=2,
    socket_connect_timeout=2,
    retry=Retry(NoBackoff(), retries=1),
)
http = httpx.AsyncClient(base_url=ORDERS_URL, timeout=3)

app = create_app()


async def cache_call(op: str, *args):
    try:
        with timed_dependency("redis"):
            return await getattr(cache, op)(*args)
    except Exception as exc:
        log.error("redis %s failed (host=%s): %s: %s", op, REDIS_HOST, type(exc).__name__, exc)
        raise HTTPException(status_code=503, detail="cache unavailable") from exc


async def orders_call(method: str, path: str, **kwargs) -> httpx.Response:
    try:
        with timed_dependency("orders"):
            resp = await http.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp
    except httpx.HTTPStatusError as exc:
        log.error("orders returned %s for %s %s", exc.response.status_code, method, path)
        raise HTTPException(status_code=502, detail="orders service error") from exc
    except httpx.HTTPError as exc:
        log.error("orders request failed (%s): %s: %s", ORDERS_URL, type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="orders service unreachable") from exc


@app.get("/api/orders")
async def list_orders():
    cached = await cache_call("get", CACHE_KEY)
    if cached:
        return json.loads(cached)
    data = (await orders_call("GET", "/orders")).json()
    await cache_call("set", CACHE_KEY, json.dumps(data), CACHE_TTL_SECONDS)
    return data


@app.post("/api/orders", status_code=201)
async def create_order(request: Request):
    resp = await orders_call("POST", "/orders", json=await request.json())
    await cache_call("delete", CACHE_KEY)
    return resp.json()
