// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

/// @title  IRuleAdapter
/// @notice Common interface for protocol-specific calldata decoders. The
///         constitution module suite (validator, hook, executor) consults
///         registered adapters to extract rule-relevant fields from
///         inner-call calldata BEFORE comparing them against the
///         constitution's rule params.
///
///         Why adapters at all? Different protocols encode the same
///         semantic operation ("open a 5x leveraged $1000 perp on ETH")
///         with very different calldata. A monolithic decoder bakes in
///         every protocol's struct layout — fragile and inevitably wrong.
///         An adapter per protocol isolates the decode logic; the
///         registry keeps an `address adapter` slot per rule so the rule
///         author picks which protocol(s) the rule applies to.
///
///         All numeric outputs are normalised:
///           sizeUsdc      — trade notional in USDC base units (1e6 = 1 USDC)
///           leverageBps   — leverage in basis points (10000 = 1x)
///           market        — protocol-specific market identifier mapped
///                           into an `address` (zero on N/A)
///         An adapter that cannot decode a given calldata MUST revert
///         with `NotApplicable(selector)` so the consumer can fall
///         through to a default (skip-rule) handler. Returning an
///         all-zero tuple is FORBIDDEN — it could mask a bypass.
interface IRuleAdapter {
    /// @notice Decoded trade descriptor. All callers must check the
    ///         relevant field for their rule kind and ignore the rest.
    struct Decoded {
        /// @notice The protocol target the adapter validated against
        ///         (e.g. the DEX router). Lets the consumer assert that
        ///         the outer execute()'s `target` matches the adapter
        ///         author's expectation. Zero == adapter is target-agnostic.
        address protocolTarget;
        /// @notice Trade size in USDC base units (1 USDC = 1e6).
        uint256 sizeUsdc;
        /// @notice Leverage in basis points (10000 = 1x).
        uint256 leverageBps;
        /// @notice Market identifier as an address. For Drift this is a
        ///         deterministic per-market PDA cast into address shape;
        ///         for GMX this is the market token address directly;
        ///         for permit/permission protocols this is the token
        ///         being permitted. Zero == not applicable.
        address market;
        /// @notice True if the decoded operation reduces position
        ///         (close/exit). Some rules whitelist exits.
        bool isReduceOnly;
    }

    /// @notice Decode `data` and return a `Decoded` descriptor. The
    ///         first 4 bytes of `data` are the inner selector; the
    ///         remaining bytes are the ABI-encoded args.
    /// @dev    Reverts `NotApplicable(selector)` if the adapter does
    ///         not recognise the selector. Reverts `Malformed()` if
    ///         the selector is recognised but the body is truncated.
    function decode(bytes calldata data) external view returns (Decoded memory);

    /// @notice The four-byte selector(s) this adapter handles. The
    ///         validator / hook indexes adapters by selector to avoid
    ///         calling every adapter on every call. Returning an empty
    ///         array means "I accept everything" (greedy).
    function selectors() external view returns (bytes4[] memory);

    /// @notice Human-readable name for logs / explorers.
    function adapterName() external view returns (string memory);

    /// @dev Standard errors. All adapters use these — keeps consumer
    ///      switch statements tight.
    error NotApplicable(bytes4 selector);
    error Malformed();
}
