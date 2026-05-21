// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

/// @title  ConstitutionRegistry
/// @notice Versioned, content-addressed storage of rule sets that govern an
///         agent's smart-account. Other contracts (notably `ConstitutionHook`)
///         look up rules by hash; the on-chain hash is the constitution's
///         canonical identifier and is also what gets pinned into memory.
/// @dev    Pure storage. No access control: anyone can publish a constitution.
///         Replaying the same rule list yields the same hash and the same
///         storage slot, so duplicates are free.
contract ConstitutionRegistry {
    /// @dev Rule kinds. Keep ordering stable - rule_id encodes the kind.
    uint8 internal constant KIND_MAX_LEVERAGE = 0;
    uint8 internal constant KIND_MAX_TRADE_SIZE = 1;
    uint8 internal constant KIND_VENUE_BLACKLIST = 2;
    uint8 internal constant KIND_NO_UNAUDITED_CONTRACTS = 3;
    uint8 internal constant KIND_SUBDELEGATION_BOUND = 4;
    uint8 internal constant KIND_CUSTOM = 255;

    struct Rule {
        uint8 kind;
        bytes params;
    }

    /// @notice constitution hash => rules
    mapping(bytes32 => Rule[]) internal _rules;
    /// @notice tracks which hashes have been defined (a defined empty set is legal)
    mapping(bytes32 => bool) public exists;

    event ConstitutionDefined(bytes32 indexed hash, uint256 ruleCount);

    error EmptyConstitution();
    error UnknownConstitution(bytes32 hash);

    /// @notice Store `rules` under its canonical content hash.
    /// @param  rules ordered rule list. Order matters for hashing.
    /// @return hash  keccak256(abi.encode(rules)) - stable per rule list.
    function defineConstitution(Rule[] calldata rules) external returns (bytes32 hash) {
        if (rules.length == 0) revert EmptyConstitution();
        hash = hashOf(rules);

        if (!exists[hash]) {
            Rule[] storage stored = _rules[hash];
            for (uint256 i = 0; i < rules.length; ++i) {
                stored.push(Rule({kind: rules[i].kind, params: rules[i].params}));
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
    ///         same array yields the same hash.
    function hashOf(Rule[] calldata rules) public pure returns (bytes32) {
        return keccak256(abi.encode(rules));
    }

    /// @notice Number of rules registered under `hash`. Zero if undefined.
    function ruleCount(bytes32 hash) external view returns (uint256) {
        return _rules[hash].length;
    }
}
