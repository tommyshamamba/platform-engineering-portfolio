// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/FlashLoanArbitrage.sol";
import "./mocks/Mocks.sol";

/// Unit tests for the recovered USDC -> WETH -> USDC receiver against local mocks.
/// Every rate below is a fixture chosen to exercise a code path, not market data.
contract FlashLoanArbitrageTest is Test {
    MockToken usdc;
    MockToken weth;
    MockAavePool pool;
    MockSwapRouter uni;
    MockSwapRouter sushi;
    FlashLoanArbitrage arb;
    address owner = address(this);
    address attacker = makeAddr("attacker");

    uint256 constant LOAN = 1_000e6; // 1,000 USDC (6 decimals)

    function setUp() public {
        usdc = new MockToken("USD Coin (mock)", "USDC", 6);
        weth = new MockToken("Wrapped Ether (mock)", "WETH", 18);
        pool = new MockAavePool();
        uni = new MockSwapRouter();
        sushi = new MockSwapRouter();

        usdc.mint(address(pool), 1_000_000e6);
        weth.mint(address(uni), 1_000e18);
        usdc.mint(address(sushi), 1_000_000e6);

        // Fixture rates: 1 USDC -> 0.0005 WETH on "uni", 1 WETH -> 2,020 USDC on "sushi".
        uni.setRate(address(usdc), address(weth), 5e26);
        sushi.setRate(address(weth), address(usdc), 2_020e6);

        arb = new FlashLoanArbitrage(address(pool), address(usdc), address(weth), address(uni), address(sushi));
    }

    function test_RoundTripRepaysLoanAndSendsSurplusToOwner() public {
        arb.executeArbitrage(LOAN);

        uint256 premium = (LOAN * pool.PREMIUM_BPS()) / 10_000; // 0.5 USDC
        uint256 back = 1_010e6; // 0.5 WETH * 2,020
        assertEq(usdc.balanceOf(owner), back - LOAN - premium, "owner receives surplus");
        assertEq(usdc.balanceOf(address(pool)), 1_000_000e6 + premium, "pool repaid with premium");
        assertEq(usdc.balanceOf(address(arb)), 0, "nothing left in receiver");
    }

    function test_RevertsWhenSecondLegCannotCoverDebt() public {
        sushi.setRate(address(weth), address(usdc), 2_000e6); // round trip returns exactly the principal
        vm.expectRevert(bytes("Too little received")); // amountOutMinimum = amount + premium
        arb.executeArbitrage(LOAN);
    }

    function test_OnlyOwnerCanStartALoan() public {
        vm.prank(attacker);
        vm.expectRevert(bytes("Only owner"));
        arb.executeArbitrage(LOAN);
    }

    function test_CallbackRejectsCallersOtherThanPool() public {
        vm.prank(attacker);
        vm.expectRevert(bytes("Invalid caller"));
        arb.executeOperation(address(usdc), LOAN, 0, address(arb), "");
    }

    function test_CallbackRejectsLoansInitiatedByOthers() public {
        // Anyone can ask the pool to lend *to* the receiver; the initiator check must stop it.
        vm.prank(attacker);
        vm.expectRevert(bytes("Invalid initiator"));
        pool.flashLoanSimple(address(arb), address(usdc), LOAN, abi.encode(LOAN), 0);
    }

    function test_WithdrawTokenIsOwnerOnly() public {
        usdc.mint(address(arb), 7e6);
        vm.prank(attacker);
        vm.expectRevert(bytes("Only owner"));
        arb.withdrawToken(address(usdc));

        arb.withdrawToken(address(usdc));
        assertEq(usdc.balanceOf(owner), 7e6);
    }
}
