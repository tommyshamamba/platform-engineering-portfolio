// SPDX-License-Identifier: MIT
// Recovered as written (July 2025, Ethereum Hardhat iteration). Only the SPDX line above was added. See README.md for known issues.
pragma solidity ^0.8.20;

import "@aave/core-v3/contracts/flashloan/interfaces/IFlashLoanSimpleReceiver.sol";
import "@aave/core-v3/contracts/interfaces/IPool.sol";
import "@aave/core-v3/contracts/interfaces/IPoolAddressesProvider.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@uniswap/v3-periphery/contracts/interfaces/ISwapRouter.sol";

contract FlashLoanArbitrage is IFlashLoanSimpleReceiver {
    address public immutable aavePool;
    address public immutable USDC;
    address public immutable WETH;
    ISwapRouter public immutable uniV3Router;
    ISwapRouter public immutable sushiV3Router;
    address public owner;
    uint24 public constant POOL_FEE = 3000; // 0.3%

    constructor(
        address _aavePool,
        address _usdc,
        address _weth,
        address _uniV3Router,
        address _sushiV3Router
    ) {
        aavePool = _aavePool;
        USDC = _usdc;
        WETH = _weth;
        uniV3Router = ISwapRouter(_uniV3Router);
        sushiV3Router = ISwapRouter(_sushiV3Router);
        owner = msg.sender;
    }

    // Required implementations for IFlashLoanSimpleReceiver
    function ADDRESSES_PROVIDER() external view override returns (IPoolAddressesProvider) {
        return IPoolAddressesProvider(aavePool);
    }

    function POOL() external view override returns (IPool) {
        return IPool(aavePool);
    }

    function executeArbitrage(uint256 amount) external {
        require(msg.sender == owner, "Only owner");
        
        bytes memory params = abi.encode(amount);
        IERC20(USDC).approve(aavePool, amount + (amount * 9 / 10000));
        
        IPool(aavePool).flashLoanSimple(
            address(this),
            USDC,
            amount,
            params,
            0
        );
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == aavePool, "Invalid caller");
        require(initiator == address(this), "Invalid initiator");

        uint256 totalDebt = amount + premium;
        
        // 1. Swap USDC->WETH on Uniswap V3
        IERC20(USDC).approve(address(uniV3Router), amount);
        uint256 wethAmount = uniV3Router.exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: USDC,
                tokenOut: WETH,
                fee: POOL_FEE,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: amount,
                amountOutMinimum: 0,
                sqrtPriceLimitX96: 0
            })
        );

        // 2. Swap WETH->USDC on Sushiswap V3
        IERC20(WETH).approve(address(sushiV3Router), wethAmount);
        uint256 finalUsdc = sushiV3Router.exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: WETH,
                tokenOut: USDC,
                fee: POOL_FEE,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: wethAmount,
                amountOutMinimum: totalDebt, // Ensure we can repay
                sqrtPriceLimitX96: 0
            })
        );

        // 3. Profit calculation and repayment
        require(finalUsdc > totalDebt, "No arbitrage profit");
        IERC20(USDC).approve(aavePool, totalDebt);
        
        // Transfer profit to owner
        uint256 profit = finalUsdc - totalDebt;
        IERC20(USDC).transfer(owner, profit);
        
        return true;
    }

    function withdrawToken(address token) external {
        require(msg.sender == owner, "Only owner");
        IERC20(token).transfer(owner, IERC20(token).balanceOf(address(this)));
    }
}
