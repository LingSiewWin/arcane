// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {IRuleAdapter} from "./IRuleAdapter.sol";

/// @title  Permit2Adapter
/// @notice Decodes Uniswap Permit2 `permitTransferFrom` calldata. This
///         is the *Arc-native* DEX-adjacent adapter: Permit2 is
///         canonically deployed on every chain at
///         `0x000000000022D473030F116dDEE9F6B43aC78BA3` — including
///         Arc testnet (verified via
///         https://docs.arc.io/arc/references/contract-addresses).
///
/// @dev    Why Permit2 and not "a DEX router"? Arc is a payment-rail
///         chain with no native perp DEX deployed today; the most
///         realistic constraint a constitution can enforce on Arc is
///         "this agent may not signature-permit more than N USDC to
///         any spender". Permit2 is the canonical signature-permit
///         primitive (Uniswap, Aerodrome, dozens of integrations);
///         signing a Permit2 message is the EVM equivalent of "letting
///         a router spend your USDC". Enforcing MAX_TRADE_SIZE on
///         Permit2 calls covers ~all DEX-style spend approvals on
///         Arc-deployed protocols.
///
///         Permit2 has two overloads — single and batch. This adapter
///         decodes the single-token overload:
///           permitTransferFrom(
///             PermitTransferFrom(TokenPermissions(token, amount),
///                                nonce, deadline),
///             SignatureTransferDetails(to, requestedAmount),
///             address owner,
///             bytes signature
///           )
///         Selector: 0x30f28b7a (computed at deploy time, see
///         `_computeSelector` for derivation).
///
///         Real selector verification: ssh into mainnet, run
///         `cast 4byte 0x30f28b7a` — returns
///         "permitTransferFrom((((address,uint256),uint256,uint256),
///          (address,uint256),address,bytes)".
///
///         Source: Uniswap/permit2 —
///         src/interfaces/ISignatureTransfer.sol.
contract Permit2Adapter is IRuleAdapter {
    /// @notice Permit2's deterministic deployment address (CREATE2 via
    ///         the canonical factory, same on every chain).
    address public constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;

    /// @dev Mirror of Permit2 structs (memory variants).
    struct TokenPermissions {
        address token;
        uint256 amount;
    }

    struct PermitTransferFrom {
        TokenPermissions permitted;
        uint256 nonce;
        uint256 deadline;
    }

    struct SignatureTransferDetails {
        address to;
        uint256 requestedAmount;
    }

    /// @notice Selector for the single-token overload.
    ///         keccak256("permitTransferFrom(((address,uint256),uint256,uint256),(address,uint256),address,bytes)")[..4]
    bytes4 internal immutable PERMIT_TRANSFER_FROM_SELECTOR;

    constructor() {
        PERMIT_TRANSFER_FROM_SELECTOR = bytes4(
            keccak256(
                "permitTransferFrom(((address,uint256),uint256,uint256),(address,uint256),address,bytes)"
            )
        );
    }

    function selectors() external view override returns (bytes4[] memory out) {
        out = new bytes4[](1);
        out[0] = PERMIT_TRANSFER_FROM_SELECTOR;
    }

    function adapterName() external pure override returns (string memory) {
        return "Permit2Adapter/permitTransferFrom";
    }

    /// @notice Decode a single-token Permit2 transferFrom call.
    function decode(bytes calldata data) external view override returns (Decoded memory d) {
        // Too short to carry a selector: NotApplicable, not Malformed (see
        // IRuleAdapter contract + GmxV2PerpAdapter.decode for rationale).
        // Lets a native-value-only userOp fall through this adapter so the
        // SUBDELEGATION_BOUND / MAX_TRADE_SIZE consumer can still inspect
        // the op rather than aborting on a forged-body error.
        if (data.length < 4) revert NotApplicable(bytes4(0));
        bytes4 sel;
        assembly {
            sel := calldataload(data.offset)
        }
        if (sel != PERMIT_TRANSFER_FROM_SELECTOR) revert NotApplicable(sel);
        if (data.length < 4 + 32) revert Malformed();

        // abi.decode validates encoding for us.
        (
            PermitTransferFrom memory permit,
            SignatureTransferDetails memory details,
            ,/* address owner */
            /* bytes signature */
        ) = abi.decode(
                data[4:],
                (PermitTransferFrom, SignatureTransferDetails, address, bytes)
            );

        // The "size" of a Permit2 transfer is the requestedAmount —
        // the actual amount the relayer will move (may be less than
        // the signed `permitted.amount` cap). We use requestedAmount
        // for MAX_TRADE_SIZE enforcement, with permitted.amount as a
        // fallback if the relayer pulls the max.
        uint256 amount = details.requestedAmount;
        if (amount == 0) {
            // Some integrations sign with `requestedAmount = 0` and
            // rely on permit.amount as the actual spend cap. Honour
            // that pattern for rule-checking.
            amount = permit.permitted.amount;
        }

        d = Decoded({
            protocolTarget: PERMIT2,
            // amount is denominated in the permitted token's base
            // units. For USDC this is 1e6 (matches the rule param
            // ABI). For other tokens the rule author is responsible
            // for using the right precision.
            sizeUsdc: amount,
            leverageBps: 0,                 // not applicable
            market: permit.permitted.token, // token being permitted
            isReduceOnly: false
        });
    }
}
