# LLM execution gate

A recovered Python module from October 2026 (`ai_arb_layer.py`). It sits in front of an Arbitrum arbitrage bot's execution step and asks a language model to return `EXECUTE` or `SKIP` for each candidate opportunity, then combines that answer with hard limits and a weighted "conviction" score. It only returns a decision. It never signs, sends or simulates a transaction, and it holds no keys. The bot it was written for (`ELYON12.py`) is not part of this repository.

## Layout
| Path | What it is |
|---|---|
| `ai_arb_layer.py` | The recovered module, verbatim (three provenance comment lines added) |
| `.env.example` | Provider, model and threshold settings. API key fields are empty placeholders; the default provider is a local Ollama server, which needs no key |
| `requirements.txt` | `aiohttp`, `python-dotenv` |
| `tests/test_ai_arb_layer.py` | 16 offline tests. The LLM call and web3 are stubbed |

## How the gate decides
1. **Floor:** opportunities below `AI_MIN_PROFIT_FLOOR_USD` are skipped without calling the model.
2. **Hourly cap:** `TradeRateLimiter` allows at most `AI_MAX_TRADES_PER_HOUR` approvals in a sliding hour.
3. **Cache:** `DecisionCache` reuses a decision for the same path, spread and size for `AI_DECISION_CACHE_TTL` seconds.
4. **Model call:** one of OpenAI, Anthropic, DeepSeek or Ollama over plain HTTP (`aiohttp`), asking for JSON. Any HTTP error, timeout, unknown provider or non-JSON reply becomes a `SKIP` (fail closed).
5. **Conviction:** `0.50 × model confidence + 0.25 × wallet signal + 0.15 × log-scaled net estimate + 0.10 × spread`, compared against `AI_AUDIT_MIN_CONFIDENCE`.
6. **Wallet signal (optional):** `SmartMoneyTracker` counts ERC-20 `Transfer` logs into a configured list of public addresses over the last N blocks and labels a path `none`, `weak` or `strong`.

The file also contains a text patcher (`--install`) that inserts the gate into `ELYON12.py` at string anchors, and `--show-patch` to print the blocks instead.

## Run
```sh
cd llm-execution-gate
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
cp .env.example .env                       # leave keys empty to use a local Ollama model
.venv/bin/python ai_arb_layer.py --show-patch
.venv/bin/python ai_arb_layer.py --test    # one synthetic opportunity through the gate
.venv/bin/python -m pytest tests -v
```
With no reachable model, `--test` logs the connection failure and returns `execute=False`, which is the intended fail-closed path.

## Findings (each has a test)
- **Cache returns a timestamp as confidence.** `DecisionCache.put` stores `(execute, time.time(), reason)`, and the auditor returns element 1 as the confidence. A cache hit therefore reports a Unix timestamp as confidence, which also inflates the conviction score past any threshold.
- **Cached approvals bypass the hourly cap.** `record()` runs only on a fresh model approval, so repeated cache hits for an approved opportunity are not counted.
- **Without a wallet signal the model must report at least 0.8 confidence.** With the default weights and a 0.65 threshold, the best case with signal `none` is `0.5 × conf + 0.25`, so a model answer at the configured 0.65 confidence can never pass on its own.
- **Patcher is only partly idempotent.** It treats the target as patched only when both the import and the audit block are present. If the audit anchor is missing, each re-run inserts the import block again.
- Not covered by tests: `SmartMoneyTracker.refresh` makes synchronous web3 calls inside an `async` method, so it blocks the event loop while it scans logs; the HTTP clients do not check status codes before reading the JSON body (the resulting exceptions are caught and treated as `SKIP`); and four imports are unused.

## Provenance
Recovered from DeepSeek chat downloads in Tommy's `Downloads` folder:
| File | What it was | Used |
|---|---|---|
| `deepseek_python_20261002_dfca2a.py` | `ai_arb_layer.py`, complete (645 lines, parses) | Yes: this folder's `ai_arb_layer.py` |
| `deepseek_python_20261003_389ea8.py` (+ identical `(1)` copy) | `arb_bot_complete.py`: same gate plus a zip bundler, cut off mid-string | No (see below) |
| `deepseek_text_20261003_b9575b.txt` | The missing tail of `389ea8`: bundler README text and CLI | No |
| `deepseek_python_20261003_188b2c.py` (+ identical `(1)` copy) | Later reformat of `arb_bot_complete.py`, also cut off mid-string | No |
| `deepseek_text_20261003_766c75.txt` | Directory tree of the local bot project | No |
| `deepseek_text_20261003_7afe14.txt` | A single local file path | No |

The gate logic (configuration, rate limiter, cache, providers, auditor, wallet signal, conviction) is the same in all three Python versions apart from formatting and log wording, so the complete October 2 file was used. The later versions import the gate from `arb_bot_complete` instead of `ai_arb_layer`, and the cache bug is present in all three. The October 3 versions only add a zip bundler that packages the local bot directory, which is not in this repository. Model names and thresholds are the original defaults. Comments in the module are the original design notes, not measured results; no trading results are claimed.
