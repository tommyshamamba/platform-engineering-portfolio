"""Reconnecting consumer for a Nitro sequencer-feed relay.

`nitro_consumer_loop` is the loop from Dec 2025 (nitro_consumer_loop.py), kept as
written apart from taking `stop_event` and `logger` as parameters instead of module
globals. Everything else in this file (the CLI and `SequenceTracker`) is glue added
so the loop can run on its own; see README.md.

It only reads the public feed. It never signs or sends anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal

import websockets


async def nitro_consumer_loop(feed_url: str, handler, stop_event: asyncio.Event, logger: logging.Logger):
    reconnect_delay = 1
    max_reconnect_delay = 60

    while not stop_event.is_set():
        try:
            async with websockets.connect(
                feed_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10,
            ) as ws:
                reconnect_delay = 1  # Reset on successful connection
                logger.info(f"Nitro feed connected to {feed_url}")

                # Process messages
                async for message in ws:
                    await handler(message)

        except (websockets.exceptions.ConnectionClosed,
                asyncio.TimeoutError,
                ConnectionError) as e:
            logger.warning(f"Nitro feed disconnected: {e}")

            # Exponential backoff
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        except Exception as e:
            logger.error(f"Nitro feed error: {e}")
            await asyncio.sleep(5)


class SequenceTracker:
    """Counts feed messages and notes gaps in `sequenceNumber`.

    Nitro broadcast frames look like {"version": 1, "messages": [{"sequenceNumber": n, ...}]}.
    Frames without that shape are counted but otherwise ignored.
    """

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.frames = 0
        self.last_seq: int | None = None
        self.gaps = 0

    async def __call__(self, raw) -> None:
        self.frames += 1
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return
        for msg in frame.get("messages") or []:
            seq = msg.get("sequenceNumber")
            if not isinstance(seq, int):
                continue
            if self.last_seq is not None and seq > self.last_seq + 1:
                self.gaps += 1
                self.logger.warning("sequence gap: %d -> %d", self.last_seq, seq)
            if self.last_seq is None or seq > self.last_seq:
                self.last_seq = seq


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.getenv("NITRO_FEED_WS", "ws://127.0.0.1:9642"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("nitro-consumer")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tracker = SequenceTracker(logger)
    consumer = asyncio.create_task(nitro_consumer_loop(args.url, tracker, stop, logger))
    await stop.wait()
    consumer.cancel()
    logger.info("frames=%d last_seq=%s gaps=%d", tracker.frames, tracker.last_seq, tracker.gaps)


if __name__ == "__main__":
    asyncio.run(_main())
