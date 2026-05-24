// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {IRuleAdapter} from "./IRuleAdapter.sol";

/// @title  DriftPerpAdapter
/// @notice Decodes Drift Protocol v2 `place_perp_order` instruction
///         calldata so the ConstitutionValidator / ConstitutionHook can
///         enforce MAX_TRADE_SIZE rules against real Drift orders.
///
/// @dev    Drift v2 is Solana / Anchor. An Anchor instruction is laid
///         out as:
///           [0..8]   8-byte instruction discriminator
///                    = sha256("global:place_perp_order")[..8]
///                    = 0x45a15dca787e4cb9
///           [8..]    Borsh-serialised `OrderParams` (little-endian):
///                      order_type            : enum (u8 tag)               1
///                      market_type           : enum (u8 tag)               1
///                      direction             : enum (u8 tag, 0=long,1=short) 1
///                      user_order_id         : u8                          1
///                      base_asset_amount     : u64 LE                      8
///                      price                 : u64 LE                      8
///                      market_index          : u16 LE                      2
///                      reduce_only           : bool (u8)                   1
///                      post_only             : enum (u8 tag)               1
///                      bit_flags             : u8                          1
///                      max_ts                : Option<i64> (1 + 8)         var
///                      trigger_price         : Option<u64> (1 + 8)         var
///                      trigger_condition     : enum (u8 tag)               1
///                      oracle_price_offset   : Option<i32> (1 + 4)         var
///                      auction_duration      : Option<u8> (1 + 1)          var
///                      auction_start_price   : Option<i64> (1 + 8)         var
///                      auction_end_price     : Option<i64> (1 + 8)         var
///
///         Source: drift-labs/protocol-v2 — programs/drift/src/state/order_params.rs
///         (Rust struct mirrored above; Borsh derives produce the byte
///         layout literally in declaration order.)
///
///         Why decode Solana bytes on an EVM chain? The Constitution
///         module suite is protocol-agnostic; agents that bridge to
///         Solana via the Drift CCTP integration sign Anchor
///         instructions and may persist the calldata bytes for
///         relay / sequencer submission. The adapter lets the
///         constitution hook reject those bytes at the EVM signing
///         step — before they're broadcast across the bridge.
///
/// @dev    Numeric normalisation.
///           BASE_PRECISION  = 1e9   (Drift base-asset precision)
///           PRICE_PRECISION = 1e6   (Drift quote precision = USDC base units)
///         Notional in USDC base units = base_asset_amount * price / 1e9.
///         Drift orders do NOT carry leverage directly (leverage =
///         position_notional / collateral, depending on the user's
///         margin account state). The adapter returns `leverageBps = 0`
///         and the MAX_LEVERAGE rule SHOULD skip Drift orders. This is
///         documented honestly rather than fabricated.
contract DriftPerpAdapter is IRuleAdapter {
    /// @notice 4-byte prefix of the Anchor `place_perp_order`
    ///         discriminator. The selectors() map keys are 4 bytes; we
    ///         re-verify the full 8 bytes inside `decode` so a
    ///         colliding 4-byte prefix can't bypass the adapter.
    bytes4 internal constant DISCRIMINATOR_PREFIX = 0x45a15dca;

    /// @notice Full 8-byte Anchor discriminator
    ///         = sha256("global:place_perp_order")[..8]
    ///         = 0x45a15dca787e4cb9.
    bytes8 internal constant DISCRIMINATOR_FULL = 0x45a15dca787e4cb9;

    /// @dev Drift uses 1e9 for base-asset precision; price is in 1e6
    ///      (USDC base units). Notional (in USDC base units) is
    ///      (base * price) / 1e9.
    uint256 internal constant BASE_PRECISION = 1e9;

    /// @dev Minimum data length required for the decode to succeed:
    ///       discriminator (8) + order_type (1) + market_type (1)
    ///     + direction (1) + user_order_id (1) + base_asset_amount (8)
    ///     + price (8) + market_index (2) + reduce_only (1)
    ///     = 31 bytes
    ///      Bytes after this (post_only, bit_flags, Option<>s) are
    ///      not needed for the fields we expose.
    uint256 internal constant MIN_LEN = 31;

    /// @notice Synthetic Drift program-id placeholder. Drift's Solana
    ///         program id is a 32-byte ed25519 pubkey; we hash a
    ///         canonical b58 string into address-shape so the adapter
    ///         can return a stable `protocolTarget`. Observers
    ///         comparing the adapter's protocolTarget against this
    ///         constant confirm "yes, the adapter author intended
    ///         this to target Drift v2".
    address public constant DRIFT_PROGRAM_ID_HINT = address(
        uint160(uint256(keccak256("drift-labs.dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH")))
    );

    function selectors() external pure override returns (bytes4[] memory out) {
        out = new bytes4[](1);
        out[0] = DISCRIMINATOR_PREFIX;
    }

    function adapterName() external pure override returns (string memory) {
        return "DriftPerpAdapter/place_perp_order";
    }

    /// @notice Decode a Drift `place_perp_order` instruction.
    /// @param  data Full instruction bytes: 8-byte discriminator
    ///              followed by the Borsh-encoded `OrderParams`.
    /// @return d   Decoded descriptor. `sizeUsdc` is the order notional
    ///             in USDC base units; `market` packs market_index
    ///             into address shape so the consumer can compare
    ///             against a per-market allowlist.
    function decode(bytes calldata data) external pure override returns (Decoded memory d) {
        if (data.length < MIN_LEN) revert Malformed();

        bytes8 disc;
        assembly {
            disc := calldataload(data.offset)
        }
        if (disc != DISCRIMINATOR_FULL) {
            // Surface the 4-byte prefix so callers can route.
            revert NotApplicable(bytes4(disc));
        }

        // Decode the fixed prefix. Borsh is little-endian; EVM
        // calldataload is big-endian. We extract each multi-byte
        // field and byte-swap into Solidity-native form.
        uint256 baseAmount  = _readU64LE(data, 12);  // discriminator(8) + 4 tag bytes
        uint256 price       = _readU64LE(data, 20);
        uint16  marketIndex = _readU16LE(data, 28);
        bool    reduceOnly  = _readBool(data, 30);

        // Notional = base * price / 1e9. base and price are u64 each
        // (fit in 64 bits); their product fits in 128 bits, so the
        // unchecked multiply never overflows uint256.
        uint256 sizeUsdc;
        unchecked {
            sizeUsdc = (baseAmount * price) / BASE_PRECISION;
        }

        d = Decoded({
            protocolTarget: DRIFT_PROGRAM_ID_HINT,
            sizeUsdc: sizeUsdc,
            leverageBps: 0, // not derivable from a single order; see contract docs
            market: address(uint160(marketIndex)),
            isReduceOnly: reduceOnly
        });
    }

    // ---- Internal little-endian primitives ----------------------------

    /// @dev Read a little-endian u64 starting at `data[offset..offset+8]`.
    function _readU64LE(bytes calldata data, uint256 offset)
        internal
        pure
        returns (uint256 v)
    {
        bytes32 word;
        assembly {
            word := calldataload(add(data.offset, offset))
        }
        // Top 8 bytes of `word` are the 8 bytes at offset, big-endian.
        // We want them interpreted as little-endian — reverse the
        // byte order.
        uint64 be = uint64(uint256(word) >> 192);
        v = uint256(_reverseU64(be));
    }

    /// @dev Read a little-endian u16 starting at `data[offset..offset+2]`.
    function _readU16LE(bytes calldata data, uint256 offset)
        internal
        pure
        returns (uint16 v)
    {
        bytes32 word;
        assembly {
            word := calldataload(add(data.offset, offset))
        }
        uint16 be = uint16(uint256(word) >> 240);
        v = (be << 8) | (be >> 8);
    }

    /// @dev Read a single byte interpreted as a bool. Any nonzero
    ///      byte is treated as `true` (matches Borsh's encoding of
    ///      `bool` even though canonical Borsh uses 0/1 only).
    function _readBool(bytes calldata data, uint256 offset)
        internal
        pure
        returns (bool v)
    {
        bytes32 word;
        assembly {
            word := calldataload(add(data.offset, offset))
        }
        v = uint8(uint256(word) >> 248) != 0;
    }

    /// @dev Reverse the byte order of a u64.
    function _reverseU64(uint64 x) internal pure returns (uint64 y) {
        y =
            ((x & 0xff00000000000000) >> 56) |
            ((x & 0x00ff000000000000) >> 40) |
            ((x & 0x0000ff0000000000) >> 24) |
            ((x & 0x000000ff00000000) >> 8)  |
            ((x & 0x00000000ff000000) << 8)  |
            ((x & 0x0000000000ff0000) << 24) |
            ((x & 0x000000000000ff00) << 40) |
            ((x & 0x00000000000000ff) << 56);
    }
}
