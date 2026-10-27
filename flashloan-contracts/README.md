# Flash-loan receiver contracts: Ethereum iteration (research prototype)

Two Aave V3 flash-loan receiver contracts from the Ethereum/Polygon Hardhat phase of this work (May to August 2025). They're kept here as a contract-security exercise, with Foundry unit tests that run against local mocks. **Nothing in this folder deploys anything, connects to a network, or reads a key, and nothing here claims any trading result.**

The later Arbitrum generation of this design (multi-lender, UUPS upgradeable, one-time callback binding) lives in [`../onchain-execution-engine/`](../onchain-execution-engine/). The two contracts below are the earlier designs it grew out of.

## Layout
| Path | What it is |
|---|---|
| `src/FlashArb.sol` | `FlashLoanSimpleReceiverBase` receiver: borrow one asset, swap to `outputToken` and back on one Uniswap V3 router (fee tier 500), require `amount + premium + minProfit` |
| `src/FlashLoanArbitrage.sol` | Owner-gated USDC → WETH → USDC round trip across two V3-style routers (Uniswap V3, then SushiSwap V3), sending any surplus to the owner |
| `test/mocks/Mocks.sol` | Test fixtures written for this folder: mintable ERC-20, an Aave-style pool that lends, calls back and pulls repayment with `transferFrom`, an addresses provider, and a fixed-rate router that pulls `tokenIn` like the real one |
| `test/*.t.sol` | 13 Foundry tests |

## Provenance and what was filled in
- Both contract bodies are as written in the original sessions. The only edits are a provenance comment at the top of each file and the missing SPDX line in `FlashLoanArbitrage.sol`.
- **Added to make it build:** `foundry.toml`, `package.json` (Aave core v3 1.19.3, OpenZeppelin 4.9.6 and Uniswap v3-core/periphery, the versions the Hardhat project used), and all of `test/`.
- The original Hardhat tests and mocks were not carried over. The last recovered `MockPool` didn't compile (a malformed `getReservesList`), and it never moved tokens, so tests built on it couldn't catch repayment bugs. The Foundry fixtures replace it.
- A later file from the same phase, `WorkingFlashLoanArbitrage`, is deliberately **excluded**. It doesn't trade. It computes "profit" from `block.timestamp`/`prevrandao` pseudo-randomness and records it, which is the kind of code that produces misleading numbers.

## Findings
The tests pin these down. Tests named `test_Finding_*` assert the defect, so they'll fail once it's fixed.
1. **`FlashArb` can never complete a loan.** The constructor approves `outputToken` to the router but never approves the borrowed asset, so the first swap's `transferFrom` reverts (`test_Finding_BorrowedAssetIsNeverApprovedToRouter`).
2. **`FlashArb.requestFlashLoan` has no access control.** Anyone can start a loan against the contract (`test_Finding_RequestFlashLoanHasNoAccessControl`). The callback's `initiator == address(this)` check doesn't help, because the contract is always the initiator.
3. **`FlashArb` has no withdraw function**, so any surplus would be stuck in the contract.
4. **`FlashArb` mixes units.** The first leg's minimum output is `amount * 99.5%` in the *borrowed* token's units, applied to `outputToken`. That's wrong whenever decimals or prices differ.
5. **`FlashLoanArbitrage` first leg has `amountOutMinimum: 0`.** Only the second leg is bounded (by `amount + premium`), so slippage protection depends on the round trip as a whole.
6. **`FlashLoanArbitrage.ADDRESSES_PROVIDER()` returns the pool address cast as a provider.** It's harmless here because nothing calls it, but it's wrong.
7. **Both use raw `transfer`/`approve` without checking return values.** That's fine for USDC/WETH, but unsafe for non-standard tokens.

What works and is tested: pool-only callbacks and initiator checks on both contracts, owner gating on `FlashLoanArbitrage`, exact repayment of principal plus premium, and the revert when the round trip can't cover the debt.

## Tests
These run offline in Foundry's local EVM: no fork URL, no RPC, no keys.
```sh
cd flashloan-contracts
npm ci --ignore-scripts   # Solidity dependencies only
forge build
forge test -vv            # 13 tests
```
Every exchange rate in the tests is a fixture picked to exercise a code path. None of them describe a market.

## Limits
These are unit tests of contract logic, **not an audit and not evidence of profitability**. Real Aave and Uniswap deployments, fees, liquidity and MEV competition aren't modeled. [onchain-execution-engine](../onchain-execution-engine/README.md#known-limitations) explains why on-chain cyclic arbitrage of this kind isn't economic for a public-RPC bot.
