// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "@aave/core-v3/contracts/interfaces/IPoolAddressesProvider.sol";
import "../src/FlashArb.sol";
import "./mocks/Mocks.sol";

/// Unit tests for the recovered FlashArb receiver. Two tests are marked "Finding":
/// they pin down defects in the recovered code rather than desired behavior.
contract FlashArbTest is Test {
    MockToken asset;
    MockToken outToken;
    MockAavePool pool;
    MockAddressesProvider provider;
    MockSwapRouter router;
    FlashArb flashArb;
    address attacker = makeAddr("attacker");

    function setUp() public {
        asset = new MockToken("Asset (mock)", "AST", 18);
        outToken = new MockToken("Output (mock)", "OUT", 18);
        pool = new MockAavePool();
        provider = new MockAddressesProvider(address(pool));
        router = new MockSwapRouter();
        asset.mint(address(pool), 1_000_000e18);
        flashArb = new FlashArb(IPoolAddressesProvider(address(provider)), address(router), address(outToken));
    }

    function test_ConstructorWiresPoolRouterAndToken() public view {
        assertEq(address(flashArb.POOL()), address(pool));
        assertEq(address(flashArb.uniswapRouter()), address(router));
        assertEq(flashArb.outputToken(), address(outToken));
        assertEq(outToken.allowance(address(flashArb), address(router)), type(uint256).max);
    }

    function test_ConstructorRejectsZeroAddresses() public {
        vm.expectRevert(bytes("Invalid router address"));
        new FlashArb(IPoolAddressesProvider(address(provider)), address(0), address(outToken));
        vm.expectRevert(bytes("Invalid token address"));
        new FlashArb(IPoolAddressesProvider(address(provider)), address(router), address(0));
    }

    function test_RequestRejectsZeroAmountAndZeroProfit() public {
        vm.expectRevert(bytes("Invalid amount"));
        flashArb.requestFlashLoan(address(asset), 0, 1, 0);
        vm.expectRevert(bytes("Invalid profit"));
        flashArb.requestFlashLoan(address(asset), 1e18, 0, 0);
    }

    function test_CallbackRejectsCallersOtherThanPool() public {
        vm.prank(attacker);
        vm.expectRevert(bytes("Unauthorized caller"));
        flashArb.executeOperation(address(asset), 1e18, 0, address(flashArb), abi.encode(uint256(1), uint160(0)));
    }

    function test_CallbackRejectsLoansInitiatedByOthers() public {
        vm.prank(attacker);
        vm.expectRevert(bytes("Unauthorized initiator"));
        pool.flashLoanSimple(address(flashArb), address(asset), 1e18, abi.encode(uint256(1), uint160(0)), 0);
    }

    /// Finding: the borrowed asset is never approved to the router (only outputToken is,
    /// in the constructor), so the first swap's transferFrom fails and no loan can complete.
    function test_Finding_BorrowedAssetIsNeverApprovedToRouter() public {
        vm.expectRevert(bytes("ERC20: insufficient allowance"));
        flashArb.requestFlashLoan(address(asset), 1_000e18, 1e18, 0);
    }

    /// Finding: requestFlashLoan has no access control. A stranger's call gets past every
    /// check and only stops at the same swap failure as the owner's call above.
    function test_Finding_RequestFlashLoanHasNoAccessControl() public {
        vm.prank(attacker);
        vm.expectRevert(bytes("ERC20: insufficient allowance"));
        flashArb.requestFlashLoan(address(asset), 1_000e18, 1e18, 0);
    }
}
