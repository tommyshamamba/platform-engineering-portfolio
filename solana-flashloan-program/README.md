# Solana flash-loan program: Anchor case study (research prototype)

An Anchor program from August 2025 that was meant to be a Solana flash-loan arbitrage system. It's kept here as an honest case study: it builds for SBF and runs in a local test bank, and the tests show what it actually does. **It has never been deployed, it borrows nothing and swaps nothing, and the "profit" it records is fixed-rate arithmetic, not trading results.**

## Layout
| Path | What it is |
|---|---|
| `programs/solana-flashloan-arbitrage/src/lib.rs` | The program as written: state account, risk limits, emergency stop, daily counters, 15 strategy IDs |
| `programs/solana-flashloan-arbitrage/tests/program.rs` | 5 tests that load the compiled `.so` into `solana-program-test` |
| `Anchor.toml`, `Cargo.toml` files | Build configuration, added for this folder |

## What the program really does
- `initialize` creates an `ArbitrageState` account with an authority and default limits: max position, daily trade limit, and a loss circuit-breaker threshold.
- `execute_flashloan_arbitrage` checks those limits, then dispatches a `strategy_id` (1 to 15, named after Raydium/Orca, Jupiter, Serum, Solend, Mango and others) to a **stub that returns a fixed fraction of `amount`**, for example `amount * 0.002`. It adds that number to `total_profit`. There's no CPI to any lending program or DEX.
- `emergency_stop`, `update_risk_parameters`, `reset_daily_metrics` and `enable_flashbots_protection` flip state flags. "Flashbots" doesn't exist on Solana, so the flag has no effect. The Solana equivalent is Jito bundles, which this program doesn't use.
- `start_arb` and `execute_swap_step` are scaffolding that does nothing.

## Findings
1. **No authority check on admin instructions.** `UpdateSettings` and `ExecuteArbitrage` require *a* signer, not the stored `authority` (no `has_one = authority`). Any wallet can change the risk limits or press the emergency stop on someone else's state account. The test `finding_any_signer_can_change_risk_parameters` asserts this.
2. **`close_arbitrage_state` lets any signer close the account and take its rent**, for the same reason.
3. **The recorded metrics are fabricated by construction.** `total_profit` and `successful_trades` come from the stubs, and `total_gas_used` is a wall-clock difference inside one instruction (always 0).
4. **The circuit breaker can't trip**, because no stub ever returns a loss.
5. `execute_large_flashloan` splits `amount` across strategies but never checks the position or daily limits.

A real version would need CPIs into a flash-loan source and DEX programs, with repayment checked in the same transaction (for example through instruction introspection), plus authority constraints. That's a rewrite, not a patch.

## Provenance and what was filled in
- `lib.rs` is byte-for-byte the last version from the original sessions, including its program ID. That ID is a public key; no keypair is in this repository.
- **Added:** the workspace and program `Cargo.toml` (anchor-lang 0.30.1, the version the sessions settled on), `Anchor.toml`, `.gitignore`, and the tests.
- Not recovered: the original Anchor.toml and any TypeScript tests.

## Build and test
Tested with Agave/Solana CLI 4.3.2, `cargo-build-sbf` 4.4.0 (platform-tools v1.57) and Rust stable. `--arch v0` is needed because the `solana-program-test` 1.18 runtime that matches anchor-lang 0.30.1 can't load the newer default SBPF v3 output.
```sh
cd solana-flashloan-program
cargo build-sbf --arch v0     # -> target/deploy/solana_flashloan_arbitrage.so
cargo test-sbf --arch v0      # 5 tests in an in-process bank; no validator, no RPC, no wallet
```
The compiler emits warnings from the original code (an unused assignment, and Anchor 0.30.1 `cfg` lints under newer Rust). They're left as is.
