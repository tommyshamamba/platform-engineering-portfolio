//! Runs the compiled SBF program in solana-program-test's in-process bank.
//! No cluster, RPC endpoint or wallet file is used; every keypair is generated per test.
//! Run with `cargo test-sbf` (it builds the .so and points BPF_OUT_DIR at it).

use anchor_lang::{AccountDeserialize, InstructionData, ToAccountMetas};
use solana_flashloan_arbitrage::{accounts, instruction, ArbitrageState, ID};
use solana_program_test::{BanksClientError, ProgramTest, ProgramTestContext};
use solana_sdk::{
    instruction::{Instruction, InstructionError},
    signature::{Keypair, Signer},
    system_program,
    transaction::{Transaction, TransactionError},
};

const ERR_SYSTEM_INACTIVE: u32 = 6000;
const ERR_INVALID_STRATEGY: u32 = 6002;
const ERR_POSITION_TOO_LARGE: u32 = 6004;

async fn start() -> ProgramTestContext {
    let mut pt = ProgramTest::new("solana_flashloan_arbitrage", ID, None);
    pt.prefer_bpf(true);
    pt.start_with_context().await
}

async fn send(ctx: &mut ProgramTestContext, ix: Instruction, signers: &[&Keypair]) -> Result<(), BanksClientError> {
    let blockhash = ctx.banks_client.get_latest_blockhash().await.unwrap();
    let mut all: Vec<&Keypair> = vec![&ctx.payer];
    all.extend_from_slice(signers);
    let tx = Transaction::new_signed_with_payer(&[ix], Some(&ctx.payer.pubkey()), &all, blockhash);
    ctx.banks_client.process_transaction(tx).await
}

fn custom_code(err: BanksClientError) -> u32 {
    match err.unwrap() {
        TransactionError::InstructionError(_, InstructionError::Custom(code)) => code,
        other => panic!("unexpected error: {other:?}"),
    }
}

/// Creates the state account with the payer as authority.
async fn init(ctx: &mut ProgramTestContext) -> Keypair {
    let state = Keypair::new();
    let ix = Instruction {
        program_id: ID,
        accounts: accounts::Initialize {
            arbitrage_state: state.pubkey(),
            authority: ctx.payer.pubkey(),
            system_program: system_program::ID,
        }
        .to_account_metas(None),
        data: instruction::Initialize {}.data(),
    };
    send(ctx, ix, &[&state]).await.unwrap();
    state
}

async fn read_state(ctx: &mut ProgramTestContext, state: &Keypair) -> ArbitrageState {
    let acct = ctx.banks_client.get_account(state.pubkey()).await.unwrap().unwrap();
    ArbitrageState::try_deserialize(&mut acct.data.as_slice()).unwrap()
}

fn execute_ix(state: &Keypair, signer: &Keypair, amount: u64, strategy_id: u8) -> Instruction {
    Instruction {
        program_id: ID,
        accounts: accounts::ExecuteArbitrage {
            arbitrage_state: state.pubkey(),
            authority: signer.pubkey(),
            system_program: system_program::ID,
        }
        .to_account_metas(None),
        data: instruction::ExecuteFlashloanArbitrage { amount, strategy_id, min_profit: 0 }.data(),
    }
}

fn settings_accounts(state: &Keypair, signer: &Keypair) -> Vec<solana_sdk::instruction::AccountMeta> {
    accounts::UpdateSettings { arbitrage_state: state.pubkey(), authority: signer.pubkey() }.to_account_metas(None)
}

#[tokio::test]
async fn initialize_sets_authority_and_default_limits() {
    let mut ctx = start().await;
    let state = init(&mut ctx).await;
    let s = read_state(&mut ctx, &state).await;
    assert_eq!(s.authority, ctx.payer.pubkey());
    assert!(s.is_active);
    assert_eq!(s.total_trades, 0);
    assert_eq!(s.daily_trade_limit, 200);
    assert_eq!(s.max_position_size, 10_000_000_000);
}

/// The strategy functions are stubs that return a fixed fraction of `amount`.
/// This test pins that down: the recorded "profit" is arithmetic, not a market result.
#[tokio::test]
async fn strategy_stub_records_a_fixed_fraction_not_a_trade() {
    let mut ctx = start().await;
    let state = init(&mut ctx).await;
    let payer = ctx.payer.insecure_clone();
    send(&mut ctx, execute_ix(&state, &payer, 1_000_000, 1), &[]).await.unwrap();
    let s = read_state(&mut ctx, &state).await;
    assert_eq!(s.total_profit, 2_000, "strategy 1 stub is amount * 0.002");
    assert_eq!(s.total_trades, 1);
    assert_eq!(s.daily_trades, 1);
}

#[tokio::test]
async fn rejects_unknown_strategy_and_oversized_position() {
    let mut ctx = start().await;
    let state = init(&mut ctx).await;
    let payer = ctx.payer.insecure_clone();
    let err = send(&mut ctx, execute_ix(&state, &payer, 1_000, 99), &[]).await.unwrap_err();
    assert_eq!(custom_code(err), ERR_INVALID_STRATEGY);
    let err = send(&mut ctx, execute_ix(&state, &payer, 10_000_000_001, 1), &[]).await.unwrap_err();
    assert_eq!(custom_code(err), ERR_POSITION_TOO_LARGE);
}

#[tokio::test]
async fn emergency_stop_blocks_execution() {
    let mut ctx = start().await;
    let state = init(&mut ctx).await;
    let payer = ctx.payer.insecure_clone();
    let stop = Instruction {
        program_id: ID,
        accounts: settings_accounts(&state, &payer),
        data: instruction::EmergencyStop {}.data(),
    };
    send(&mut ctx, stop, &[]).await.unwrap();
    let err = send(&mut ctx, execute_ix(&state, &payer, 1_000, 1), &[]).await.unwrap_err();
    assert_eq!(custom_code(err), ERR_SYSTEM_INACTIVE);
}

/// Finding: UpdateSettings only requires *a* signer, not the stored authority
/// (there is no `has_one = authority` constraint). Any wallet can change the risk
/// limits or flip the emergency stop on someone else's state account.
#[tokio::test]
async fn finding_any_signer_can_change_risk_parameters() {
    let mut ctx = start().await;
    let state = init(&mut ctx).await;
    let stranger = Keypair::new();
    let ix = Instruction {
        program_id: ID,
        accounts: settings_accounts(&state, &stranger),
        data: instruction::UpdateRiskParameters {
            circuit_breaker_threshold: 0,
            max_position_size: u64::MAX,
            daily_trade_limit: u32::MAX,
        }
        .data(),
    };
    send(&mut ctx, ix, &[&stranger]).await.unwrap();
    let s = read_state(&mut ctx, &state).await;
    assert_ne!(s.authority, stranger.pubkey());
    assert_eq!(s.max_position_size, u64::MAX, "stranger's update was accepted");
}
