// SPDX-License-Identifier: MIT
// Test fixtures written for this folder (not recovered from the chats).
// They model only what the two receivers touch: Aave V3 flashLoanSimple
// (lend, call back, pull amount + premium with transferFrom) and a Uniswap V3
// style router that pulls tokenIn with transferFrom and pays a fixed rate.
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@uniswap/v3-periphery/contracts/interfaces/ISwapRouter.sol";

contract MockToken is ERC20 {
    uint8 private immutable _dec;
    constructor(string memory n, string memory s, uint8 d) ERC20(n, s) { _dec = d; }
    function decimals() public view override returns (uint8) { return _dec; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

interface IFlashReceiver {
    function executeOperation(address asset, uint256 amount, uint256 premium, address initiator, bytes calldata params)
        external returns (bool);
}

/// Minimal stand-in for the Aave V3 Pool flashLoanSimple flow.
contract MockAavePool {
    uint256 public constant PREMIUM_BPS = 5; // fixture value; Aave's real premium is governance-set

    function flashLoanSimple(address receiver, address asset, uint256 amount, bytes calldata params, uint16)
        external
    {
        uint256 premium = (amount * PREMIUM_BPS) / 10_000;
        require(ERC20(asset).transfer(receiver, amount), "lend failed");
        require(
            IFlashReceiver(receiver).executeOperation(asset, amount, premium, msg.sender, params),
            "callback returned false"
        );
        require(ERC20(asset).transferFrom(receiver, address(this), amount + premium), "repay failed");
    }
}

/// Only getPool() is used by FlashLoanSimpleReceiverBase's constructor.
contract MockAddressesProvider {
    address public immutable pool;
    constructor(address p) { pool = p; }
    function getPool() external view returns (address) { return pool; }
}

/// Uniswap V3 style router: pulls tokenIn, pays out amountIn * rate / 1e18 of tokenOut.
contract MockSwapRouter {
    mapping(address => mapping(address => uint256)) public rate; // tokenIn => tokenOut => 1e18-scaled

    function setRate(address tokenIn, address tokenOut, uint256 r) external { rate[tokenIn][tokenOut] = r; }

    function exactInputSingle(ISwapRouter.ExactInputSingleParams calldata p) external payable returns (uint256 out) {
        require(ERC20(p.tokenIn).transferFrom(msg.sender, address(this), p.amountIn), "pull tokenIn failed");
        out = (p.amountIn * rate[p.tokenIn][p.tokenOut]) / 1e18;
        require(out >= p.amountOutMinimum, "Too little received");
        require(ERC20(p.tokenOut).transfer(p.recipient, out), "pay tokenOut failed");
    }
}
