"""frontend: renders the shop page from the api."""

import html
import os

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from shop.common import create_app, setup_logging, timed_dependency

log = setup_logging()

API_URL = os.environ["API_URL"]
http = httpx.AsyncClient(base_url=API_URL, timeout=3)

app = create_app()

PAGE = """<!doctype html>
<html><head><title>kubebreaker shop</title></head>
<body><h1>kubebreaker shop</h1>{body}</body></html>"""


async def api_call(method: str, path: str, **kwargs) -> httpx.Response | None:
    try:
        with timed_dependency("api"):
            resp = await http.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp
    except httpx.HTTPStatusError as exc:
        log.error("api returned %s for %s %s", exc.response.status_code, method, path)
    except httpx.HTTPError as exc:
        log.error("api request failed (%s): %s: %s", API_URL, type(exc).__name__, exc)
    return None


@app.get("/", response_class=HTMLResponse)
async def index():
    resp = await api_call("GET", "/api/orders")
    if resp is None:
        return HTMLResponse(PAGE.format(body="<p>Something went wrong.</p>"), status_code=502)
    rows = "".join(
        f"<li>#{o['id']} {html.escape(o['item'])} x{o['qty']}</li>" for o in resp.json()["orders"]
    )
    return PAGE.format(body=f"<h2>Recent orders</h2><ul>{rows}</ul>")


@app.post("/order")
async def place_order(request: Request):
    resp = await api_call("POST", "/api/orders", json=await request.json())
    if resp is None:
        return JSONResponse({"error": "could not place order"}, status_code=502)
    return JSONResponse(resp.json(), status_code=201)
