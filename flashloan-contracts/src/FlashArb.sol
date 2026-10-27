// SPDX-License-Identifier: MIT
// Recovered as written (July 2025, Ethereum/Polygon Hardhat iteration). Research code: see README.md for known issues.
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@aave/core-v3/contracts/flashloan/base/FlashLoanSimpleReceiverBase.sol";
import "@uniswap/v3-periphery/contracts/interfaces/ISwapRouter.sol";

contract FlashArb is FlashLoanSimpleReceiverBase {
    ISwapRouter public immutable uniswapRouter;
    address public immutable outputToken;
    uint24 public constant POOL_FEE = 500;

    constructor(
        IPoolAddressesProvider provider,
        address _uniswapRouter,
        address _outputToken
    ) FlashLoanSimpleReceiverBase(provider) {
        require(_uniswapRouter != address(0), "Invalid router address");
        require(_outputToken != address(0), "Invalid token address");

        uniswapRouter = ISwapRouter(_uniswapRouter);
        outputToken = _outputToken;

        // Raw approve (reset + max)
        IERC20(_outputToken).approve(_uniswapRouter, 0);
        IERC20(_outputToken).approve(_uniswapRouter, type(uint256).max);
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == address(POOL), "Unauthorized caller");
        require(initiator == address(this), "Unauthorized initiator");

        (uint256 minProfit, uint160 sqrtPriceLimit) = abi.decode(params, (uint256, uint160));
        require(minProfit > 0, "Invalid min profit");

        // First swap: asset -> outputToken
        uint256 amountOut = _executeSwap(
            asset,
            outputToken,
            amount,
            _calculateMinAmountOut(amount, 50),
            sqrtPriceLimit
        );

        // Second swap: outputToken -> asset
        uint256 finalAmount = _executeSwap(
            outputToken,
            asset,
            amountOut,
            amount + premium + minProfit,
            sqrtPriceLimit
        );

        require(finalAmount >= amount + premium + minProfit, "Insufficient profit");

        // Raw approve for loan repayment
        IERC20(asset).approve(address(POOL), 0);
        IERC20(asset).approve(address(POOL), amount + premium);

        return true;
    }

    function requestFlashLoan(
        address asset,
        uint256 amount,
        uint256 minProfit,
        uint160 sqrtPriceLimit
    ) external {
        require(amount > 0, "Invalid amount");
        require(minProfit > 0, "Invalid profit");

        bytes memory params = abi.encode(minProfit, sqrtPriceLimit);
        POOL.flashLoanSimple(address(this), asset, amount, params, 0);
    }

    function _executeSwap(
        address tokenIn,
        address tokenOutParam,
        uint256 amountIn,
        uint256 minAmountOut,
        uint160 sqrtPriceLimit
    ) internal returns (uint256) {
        return uniswapRouter.exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: tokenIn,
                tokenOut: tokenOutParam,
                fee: POOL_FEE,
                recipient: address(this),
                deadline: block.timestamp + 300,
                amountIn: amountIn,
                amountOutMinimum: minAmountOut,
                sqrtPriceLimitX96: sqrtPriceLimit
            })
        );
    }

    function _calculateMinAmountOut(uint256 amount, uint256 slippageBps) internal pure returns (uint256) {
        require(slippageBps <= 100, "Slippage too high");
        return (amount * (10_000 - slippageBps)) / 10_000;
    }
}
