// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {IRuleAdapter} from "./IRuleAdapter.sol";

/// @title  GmxV2PerpAdapter
/// @notice Decodes GMX v2 `ExchangeRouter.createOrder(CreateOrderParams)`
///         calldata so the ConstitutionValidator / ConstitutionHook can
///         enforce MAX_TRADE_SIZE / MAX_LEVERAGE rules against real GMX
///         perp orders.
///
/// @dev    GMX v2 is the canonical real-world EVM perp protocol with a
///         publicly documented calldata layout. We reproduce the
///         `IBaseOrderUtils.CreateOrderParams` struct here so the
///         decoder can rely on `abi.decode` rather than hand-walking
///         offsets — Solidity's ABI codec validates lengths for us.
///
///         Selector:
///           keccak256("createOrder((CreateOrderParamsAddresses,CreateOrderParamsNumbers,uint8,uint8,bool,bool,bool,bytes32,bytes32[]))")[..4]
///         Concretely with the full nested type:
///           createOrder(((address,address,address,address,address,address,address[]),(uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256),uint8,uint8,bool,bool,bool,bytes32,bytes32[]))
///         Selector computed at deployment by `_computeSelector()`
///         (pure) — we don't hard-code a hex literal so that anyone
///         who reproduces the build can verify the selector locally.
///
///         Source: gmx-io/gmx-synthetics —
///           contracts/router/ExchangeRouter.sol
///           contracts/order/IBaseOrderUtils.sol
///
/// @dev    Leverage derivation.
///         GMX v2 uses USD-denominated sizes:
///           sizeDeltaUsd               (1e30 precision)
///           initialCollateralDeltaAmount (in collateral token base units)
///         To compute leverage_bps we'd need the collateral token's USD
///         price — out of scope for a pure decoder. We expose a
///         conservative APPROXIMATION:
///           leverageBps = sizeDeltaUsd / initialCollateralDeltaAmount * (10000 * COLLATERAL_SCALE / 1e30)
///         where COLLATERAL_SCALE = 1e6 for USDC. This is accurate when
///         collateral is USDC (the most common case on GMX v2). For
///         non-USDC collateral the consumer SHOULD set a venue
///         blacklist rather than rely on this approximation; the
///         adapter documents the assumption with `usdcCollateralScale`.
contract GmxV2PerpAdapter is IRuleAdapter {
    /// @notice GMX v2 collateral-token precision assumption. USDC = 1e6.
    ///         If your collateral is WETH (1e18) the leverage estimate
    ///         is meaningless; rule authors should explicitly blacklist
    ///         non-USDC collateral.
    uint256 internal constant COLLATERAL_SCALE = 1e6;

    /// @notice GMX v2 USD precision is 1e30. We divide sizeDeltaUsd by
    ///         1e24 to convert to USDC base units (1e6).
    uint256 internal constant USD_TO_USDC = 1e24;

    /// @dev Mirror of `IBaseOrderUtils.CreateOrderParams`.
    struct CreateOrderParamsAddresses {
        address receiver;
        address cancellationReceiver;
        address callbackContract;
        address uiFeeReceiver;
        address market;
        address initialCollateralToken;
        address[] swapPath;
    }

    struct CreateOrderParamsNumbers {
        uint256 sizeDeltaUsd;
        uint256 initialCollateralDeltaAmount;
        uint256 triggerPrice;
        uint256 acceptablePrice;
        uint256 executionFee;
        uint256 callbackGasLimit;
        uint256 minOutputAmount;
        uint256 validFromTime;
    }

    struct CreateOrderParams {
        CreateOrderParamsAddresses addresses;
        CreateOrderParamsNumbers numbers;
        uint8  orderType;                  // Order.OrderType enum
        uint8  decreasePositionSwapType;   // Order.DecreasePositionSwapType
        bool   isLong;
        bool   shouldUnwrapNativeToken;
        bool   autoCancel;
        bytes32 referralCode;
        bytes32[] dataList;
    }

    /// @notice ExchangeRouter address on Arbitrum mainnet (where GMX v2
    ///         is canonically deployed). Used as `protocolTarget` so a
    ///         rule author can pin "only orders going to this router
    ///         are subject to this adapter's decode".
    ///         Source: https://gmxio.gitbook.io/gmx/contracts
    address public constant GMX_V2_EXCHANGE_ROUTER =
        0x7C68C7866A64FA2160F78EEaE12217FFbf871fa8;

    /// @notice 4-byte selector for createOrder(CreateOrderParams).
    bytes4 internal immutable CREATE_ORDER_SELECTOR;

    constructor() {
        // We compute the selector at deployment from the canonical
        // type string. This means anyone reading the contract can
        // re-derive the selector by hashing the SAME string — no
        // chance of a "mystery hex literal".
        CREATE_ORDER_SELECTOR = bytes4(
            keccak256(
                "createOrder(((address,address,address,address,address,address,address[]),(uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256),uint8,uint8,bool,bool,bool,bytes32,bytes32[]))"
            )
        );
    }

    function selectors() external view override returns (bytes4[] memory out) {
        out = new bytes4[](1);
        out[0] = CREATE_ORDER_SELECTOR;
    }

    function adapterName() external pure override returns (string memory) {
        return "GmxV2PerpAdapter/createOrder";
    }

    /// @notice Decode a GMX v2 `createOrder` call.
    /// @param  data full calldata, selector-prefixed.
    function decode(bytes calldata data) external view override returns (Decoded memory d) {
        // Too short to even carry a selector: this adapter cannot
        // recognise it. Per IRuleAdapter, that's NotApplicable (the
        // consumer falls through), NOT Malformed — Malformed is reserved
        // for "selector is mine but the body is truncated" (a forgery).
        // This matters for the inline MAX_TRADE_SIZE native-value path:
        // a `value`-only userOp carries empty inner calldata, and the
        // (earlier-running) MAX_LEVERAGE adapter must not abort the whole
        // validation before MAX_TRADE_SIZE gets to inspect `value`.
        if (data.length < 4) revert NotApplicable(bytes4(0));
        bytes4 sel;
        assembly {
            sel := calldataload(data.offset)
        }
        if (sel != CREATE_ORDER_SELECTOR) revert NotApplicable(sel);
        if (data.length < 4 + 32) revert Malformed();

        // abi.decode validates body length for us — if the encoded
        // payload is truncated mid-struct, it reverts. We catch a
        // shallow length check above to surface our typed
        // `Malformed()` error early for the common short-data case.
        CreateOrderParams memory p = abi.decode(data[4:], (CreateOrderParams));

        // Notional in USDC base units. sizeDeltaUsd is 1e30; divide
        // by 1e24 to get 1e6 (USDC base units).
        uint256 sizeUsdc = p.numbers.sizeDeltaUsd / USD_TO_USDC;

        // Leverage estimate (assumes USDC collateral; see contract docs).
        // leverageBps = (sizeUsdc * 10000) / collateralUsdc
        // To preserve precision we compute as:
        //   leverageBps = (sizeDeltaUsd * 10000) / (initialCollateralDeltaAmount * 1e24)
        // which is the same algebraically.
        uint256 leverageBps;
        uint256 collateral = p.numbers.initialCollateralDeltaAmount;
        if (collateral > 0) {
            // Use the USDC-base-units form so the result is independent
            // of whether the math rounds in mul or div first.
            // sizeUsdc * 10_000 fits comfortably in 256 bits: GMX v2
            // sizeDeltaUsd has a hard cap of ~1e36 (well below 2**128),
            // so sizeUsdc ≤ 1e12 and * 10_000 ≤ 1e16.
            leverageBps = (sizeUsdc * 10_000) / collateral;
        }

        d = Decoded({
            protocolTarget: GMX_V2_EXCHANGE_ROUTER,
            sizeUsdc: sizeUsdc,
            leverageBps: leverageBps,
            market: p.addresses.market,
            // GMX uses orderType to flag decrease orders. The canonical
            // mapping is:
            //   0 MarketSwap, 1 LimitSwap, 2 MarketIncrease,
            //   3 LimitIncrease, 4 MarketDecrease, 5 LimitDecrease,
            //   6 StopLossDecrease, 7 Liquidation
            // The decrease family is orderType >= 4 && orderType <= 6.
            isReduceOnly: p.orderType >= 4 && p.orderType <= 6
        });
    }
}
