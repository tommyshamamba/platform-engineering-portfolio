"""Offline tests: a local WebSocket server stands in for the relay. No network access."""

import asyncio
import json
import logging
import sys
import unittest
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "consumer"))
import nitro_consumer  # noqa: E402

LOG = logging.getLogger("test")


def frame(*seqs):
    return json.dumps({"version": 1, "messages": [{"sequenceNumber": s} for s in seqs]})


class ConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnects_after_server_drops_connection(self):
        connections = 0

        async def relay(ws):
            nonlocal connections
            connections += 1
            if connections == 1:
                await ws.send(frame(10, 11))
                await ws.close()  # simulate the EOFs seen in the relay logs
            else:
                await ws.send(frame(12))
                await asyncio.sleep(5)

        stop = asyncio.Event()
        received = []

        async def handler(msg):
            received.append(msg)
            if len(received) == 2:
                stop.set()

        async with websockets.serve(relay, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            task = asyncio.create_task(
                nitro_consumer.nitro_consumer_loop(f"ws://127.0.0.1:{port}", handler, stop, LOG)
            )
            await asyncio.wait_for(stop.wait(), timeout=10)
            task.cancel()

        self.assertEqual(connections, 2)
        self.assertEqual(received, [frame(10, 11), frame(12)])

    async def test_backs_off_while_relay_is_down(self):
        stop = asyncio.Event()
        task = asyncio.create_task(
            nitro_consumer.nitro_consumer_loop("ws://127.0.0.1:9", lambda m: None, stop, LOG)
        )
        await asyncio.sleep(0.5)
        self.assertFalse(task.done(), "loop keeps retrying instead of crashing")
        stop.set()
        task.cancel()

    async def test_sequence_tracker_counts_gaps(self):
        t = nitro_consumer.SequenceTracker(LOG)
        for raw in (frame(1, 2), frame(3), frame(7), "not json", frame(8)):
            await t(raw)
        self.assertEqual(t.frames, 5)
        self.assertEqual(t.last_seq, 8)
        self.assertEqual(t.gaps, 1)


if __name__ == "__main__":
    unittest.main()
