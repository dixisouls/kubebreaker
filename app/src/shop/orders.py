"""orders: stores orders in Postgres."""

import os
from contextlib import asynccontextmanager

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from shop.common import create_app, setup_logging, timed_dependency

log = setup_logging()

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

pool = ConnectionPool(
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} "
    f"password={DB_PASSWORD} connect_timeout=3",
    min_size=1,
    max_size=5,
    timeout=3,
    open=False,
)


@asynccontextmanager
async def lifespan(_app):
    log.info("connecting to postgres at %s:%s/%s", DB_HOST, DB_PORT, DB_NAME)
    pool.open(wait=False)  # keep retrying in the background if the DB isn't up yet
    yield
    pool.close()


app = create_app(lifespan=lifespan)


class NewOrder(BaseModel):
    item: str = Field(min_length=1, max_length=100)
    qty: int = Field(ge=1, le=100)


def run_query(sql: str, params: tuple = ()) -> list[dict]:
    try:
        with timed_dependency("postgres"), pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []
    except Exception as exc:
        log.error("database error (host=%s): %s: %s", DB_HOST, type(exc).__name__, exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc


@app.get("/orders")
def list_orders():
    rows = run_query("SELECT id, item, qty, created_at FROM orders ORDER BY id DESC LIMIT 20")
    return {"orders": rows}


@app.post("/orders", status_code=201)
def create_order(order: NewOrder):
    rows = run_query(
        "INSERT INTO orders (item, qty) VALUES (%s, %s) RETURNING id", (order.item, order.qty)
    )
    return {"id": rows[0]["id"]}
