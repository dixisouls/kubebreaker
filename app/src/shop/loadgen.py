"""loadgen: steady traffic against the frontend so metrics always have data."""

import logging
import os
import random
import signal
import sys
import time

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s loadgen %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("loadgen")

TARGET = os.environ.get("TARGET_URL", "http://frontend")
INTERVAL = float(os.environ.get("INTERVAL_SECONDS", "0.5"))
REPORT_EVERY = 30.0
ITEMS = ["widget", "gadget", "gizmo", "doohickey", "sprocket"]


def main() -> None:
    # As PID 1 we must handle SIGTERM ourselves, or the pod takes the full grace period to stop
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("sending traffic to %s every %.1fs", TARGET, INTERVAL)
    ok = failed = 0
    last_report = time.monotonic()
    with httpx.Client(base_url=TARGET, timeout=5) as client:
        while True:
            try:
                if random.random() < 0.1:
                    order = {"item": random.choice(ITEMS), "qty": random.randint(1, 5)}
                    resp = client.post("/order", json=order)
                else:
                    resp = client.get("/")
                ok, failed = (ok + 1, failed) if resp.is_success else (ok, failed + 1)
            except httpx.HTTPError:
                failed += 1
            if time.monotonic() - last_report >= REPORT_EVERY:
                log.info("last %.0fs: ok=%d failed=%d", REPORT_EVERY, ok, failed)
                ok = failed = 0
                last_report = time.monotonic()
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
