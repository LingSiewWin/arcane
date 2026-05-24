// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

/// @title  ConstitutionRegistry
/// @notice Versioned, content-addressed storage of rule sets that govern an
///         agent's smart-account. Other contracts (ConstitutionValidator,
///         ConstitutionHook, ConstitutionExecutor) look up rules by hash;
///         the on-chain hash is the constitution's canonical identifier
///         and is also what gets pinned into memory.
/// @dev    Pure storage. No access control: anyone can publish a
///         constitution. Replaying the same rule list yields the same hash
///         and the same storage slot, so duplicates are free.
///
///         Phase 5 Stream M change: each rule gains an `address adapter`
///         field. When non-zero, the validator/hook routes the inner-call
///         calldata through that adapter to extract rule-relevant fields
///         (size, leverage, market) instead of decoding inline. This is
///         how MAX_LEVERAGE / MAX_TRADE_SIZE are enforced against real
///         protocols (Drift, GMX v2, Permit2) without baking selectors
///         into the validator.
///
///         The rule hash is now `keccak256(abi.encode(rules))` over the
///         3-field Rule, so any constitution defined under the pre-Phase-5
///         2-field schema produces a DIFFERENT hash here — by design. The
///         registry is the source of truth; off-chain seed code re-anchors
///         under the new schema.
contract ConstitutionRegistry {
    /// @dev Rule kinds. Keep ordering stable - rule_id encodes the kind.
    uint8 internal constant KIND_MAX_LEVERAGE = 0;
    uint8 internal constant KIND_MAX_TRADE_SIZE = 1;
    uint8 internal constant KIND_VENUE_BLACKLIST = 2;
    uint8 internal constant KIND_NO_UNAUDITED_CONTRACTS = 3;
    uint8 internal constant KIND_SUBDELEGATION_BOUND = 4;
    uint8 internal constant KIND_CUSTOM = 255;

    /// @notice A single constitution rule.
    /// @dev    `adapter` is an IRuleAdapter address. When zero, the
    ///         validator/hook falls back to inline decoding (kept for the
    ///         MAX_TRADE_SIZE ERC-20-transfer fast path; see
    ///         ConstitutionValidator._checkMaxTradeSize). When non-zero,
    ///         the validator calls `IRuleAdapter(adapter).decode(inner)`
    ///         and compares the decoded fields against `params`.
    struct Rule {
        uint8 kind;
        bytes params;
        address adapter;
    }

    /// @notice constitution hash => rules
    mapping(bytes32 => Rule[]) internal _rules;
    /// @notice tracks which hashes have been defined (a defined empty set is legal)
    mapping(bytes32 => bool) public exists;

    event ConstitutionDefined(bytes32 indexed hash, uint256 ruleCount);

    error EmptyConstitution();
    error UnknownConstitution(bytes32 hash);

    /// @notice A rule whose `kind` cannot be enforced without an adapter
    ///         was registered with `adapter == address(0)`.
    /// @dev    B16 fix. Previously the ConstitutionValidator silently
    ///         no-op'd such rules (`if (r.adapter == address(0)) return;`),
    ///         a fail-open: a deployer could publish a constitution
    ///         believing leverage / sub-delegation was capped while the
    ///         cap did nothing. We now reject at REGISTRATION so the
    ///         misconfiguration surfaces immediately and can never be
    ///         installed.
    error AdapterRequired(uint8 kind);

    /// @notice Returns true if rules of `kind` are unenforceable without a
    ///         non-zero `IRuleAdapter`. This is the canonical kind ->
    ///         adapter-requirement mapping; it MUST stay in lock-step with
    ///         `ConstitutionValidator._enforce`'s dispatch.
    ///
    ///         kind -> adapter required?
    ///           0  MAX_LEVERAGE           : YES — leverage is only
    ///                                       derivable by an adapter
    ///                                       decoding real perp calldata
    ///                                       (GmxV2PerpAdapter). With no
    ///                                       adapter the validator has no
    ///                                       leverage to compare against.
    ///           1  MAX_TRADE_SIZE         : NO  — has an inline fast path
    ///                                       (native `value` + ERC-20
    ///                                       `transfer(address,uint256)`).
    ///                                       Adapter is OPTIONAL (extends
    ///                                       coverage to DEX/perp calls).
    ///           2  VENUE_BLACKLIST        : NO  — inline target compare.
    ///           3  NO_UNAUDITED_CONTRACTS : NO  — inline target compare.
    ///           4  SUBDELEGATION_BOUND    : YES — the proposed child
    ///                                       allowance is only readable via
    ///                                       an adapter decoding the
    ///                                       sub-delegation calldata.
    ///           255 CUSTOM / unknown      : NO  — no-op in the validator.
    function _adapterRequired(uint8 kind) internal pure returns (bool) {
        return kind == KIND_MAX_LEVERAGE || kind == KIND_SUBDELEGATION_BOUND;
    }

    /// @notice Store `rules` under its canonical content hash.
    /// @param  rules ordered rule list. Order matters for hashing.
    /// @return hash  keccak256(abi.encode(rules)) - stable per rule list.
    function defineConstitution(Rule[] calldata rules) external returns (bytes32 hash) {
        if (rules.length == 0) revert EmptyConstitution();

        // B16: fail closed at registration. An adapter-requiring rule with
        // a zero adapter is silently unenforceable in the validator, so we
        // reject it here — the constitution can never reach onInstall.
        for (uint256 i = 0; i < rules.length; ++i) {
            if (_adapterRequired(rules[i].kind) && rules[i].adapter == address(0)) {
                revert AdapterRequired(rules[i].kind);
            }
        }

        hash = hashOf(rules);

        if (!exists[hash]) {
            Rule[] storage stored = _rules[hash];
            for (uint256 i = 0; i < rules.length; ++i) {
                stored.push(Rule({
                    kind: rules[i].kind,
                    params: rules[i].params,
                    adapter: rules[i].adapter
                }));
            }
            exists[hash] = true;
            emit ConstitutionDefined(hash, rules.length);
        }
    }

    /// @notice Read back a previously-defined constitution.
    function getConstitution(bytes32 hash) external view returns (Rule[] memory) {
        if (!exists[hash]) revert UnknownConstitution(hash);
        return _rules[hash];
    }

    /// @notice Compute the canonical hash for a rule array without writing.
    /// @dev    keccak256 over the abi-encoded array; calling twice with the
    ///         same array yields the same hash. Note this is the 3-field
    ///         Phase-5 schema — rule lists serialised under the previous
    ///         2-field schema (kind, params) produce a different hash.
    function hashOf(Rule[] calldata rules) public pure returns (bytes32) {
        return keccak256(abi.encode(rules));
    }

    /// @notice Number of rules registered under `hash`. Zero if undefined.
    function ruleCount(bytes32 hash) external view returns (uint256) {
        return _rules[hash].length;
    }
}
