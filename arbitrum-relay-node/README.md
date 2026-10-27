# Arbitrum Nitro relay node

Configuration and tooling for running Offchain Labs' Nitro **feed relay** locally, from November and December 2025. The relay subscribes to the public Arbitrum One sequencer feed and rebroadcasts it on `ws://127.0.0.1:9642`, so local consumers share one upstream connection. It holds no keys and sends no transactions.

## Layout
| Path | What it is |
|---|---|
| `docker-compose.yml` | The relay service: image `offchainlabs/nitro-node:v3.9.2-52e8959`, `relay` entrypoint, Arbitrum One feed, chain ID 42161 |
| `.env.example` | Image, feed URL, port and watchdog settings. None of them are secret |
| `scripts/relay_watchdog.sh` | Restarts the container when the port stops accepting connections, or when a consumer log shows a stall marker. `--once` runs a single check |
| `scripts/relay_monitor.sh` | Interactive troubleshooting loop: container state, memory, port, optional WebSocket handshake, upstream reachability and recent log errors |
| `consumer/nitro_consumer.py` | Reconnecting WebSocket consumer with exponential backoff, plus a sequence-number gap counter |
| `tests/test_consumer.py` | 3 offline tests against a local WebSocket server |

## Run
```sh
cd arbitrum-relay-node
cp .env.example .env
docker compose up -d
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python consumer/nitro_consumer.py --url ws://127.0.0.1:9642
./scripts/relay_watchdog.sh        # optional supervisor loop
```

## What the logs showed
The relay ran in Docker from about 25 November to 15 December 2025. Its own logs from that period show the upstream connection dropping with `i/o timeout` or `readData returned EOF`. Nitro then reconnected and resumed with a newer `requestedSeqNum`, logging that the gap was removed from its backlog. Downstream, a consumer on `ws://127.0.0.1:9642` saw `no close frame received or sent` disconnects and reconnected within about a second. That is why the watchdog and the backoff loop exist. No uptime or latency figures were measured, so none are given.

## Provenance and what was changed
- **`docker-compose.yml`** comes from the `docker run` command that was actually used. Changes: it dropped `network_mode: host`, which conflicts with `ports:` in the recovered compose file. It binds the port to `127.0.0.1`, and it adds `restart: unless-stopped`, log rotation and `.env` variables.
- **`relay_watchdog.sh`** comes from `run_relay.sh`. Fixes:
  - It probed `http://localhost:9642/health`, which the relay isn't known to serve. It now uses a TCP connect.
  - It restarted a service named `nitro-relay`, which didn't match the compose service.
  - It recorded `$!` of a backgrounded `docker-compose restart` as the "relay PID".
  - It had a hard-coded placeholder log path.
  - It now passes shellcheck.
- **`relay_monitor.sh`** is the script body from inside a heredoc in `monitor_relay_wrapper.sh`. It uses `docker ps`/`docker stats` instead of `ps | grep` and `sudo lsof`, and it skips `websocat` when it isn't installed.
- **`nitro_consumer.py`:** the loop is as written, except that `STOP_EVENT` and `logger` became parameters. The imports, the CLI and `SequenceTracker` were added so it runs on its own.
- **Not included:** `health_check.py`. It was a fragment of the trading bot (it awaited a synchronous web3 call) and doesn't belong to the relay.

## Verification
```sh
docker compose config -q                      # compose file validates (with and without .env)
shellcheck scripts/*.sh                       # clean
python -m unittest discover -s tests -v       # 3 tests: reconnect after drop, backoff while down, gap counting
```
Live check (27 October 2026): `docker compose up` pulled the image, the relay logged `Feed connected` to `wss://arb1-feed.arbitrum.io/feed`, `relay_watchdog.sh --once` exited 0, and the consumer received feed frames on `ws://127.0.0.1:9642` with no sequence gaps. It exited 1 again after `docker compose down`. The test machine only allowed outbound traffic from the host network, so that run used a local override with `network_mode: host`; the committed bridge-network file was not tested against the live feed.
