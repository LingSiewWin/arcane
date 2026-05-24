// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {
    IValidator,
    PackedUserOperation,
    MODULE_TYPE_VALIDATOR,
    VALIDATION_SUCCESS
} from "./interfaces/IERC7579.sol";
import {ConstitutionRegistry} from "./ConstitutionRegistry.sol";
import {IRuleAdapter} from "./adapters/IRuleAdapter.sol";

/// @title  ConstitutionValidator
/// @notice ERC-7579 *validator* module (MODULE_TYPE_VALIDATOR = 1) that
///         re-checks every user operation against the agent's published
///         constitution and reverts on violation.
///
///         History note. This contract was previously called
///         `ConstitutionHook` — a misnomer flagged in the Phase-4 audit
///         (`docs/audit_phase4_review.md` finding B7 and
///         `docs/benchmark_realworld.md` §4). It returns
///         `VALIDATION_SUCCESS` from `validateUserOp` and never
///         implements `preCheck`/`postCheck`, so it is structurally a
///         validator under ERC-7579's taxonomy. Phase 5 Stream M
///         renames the file and ships a *new*, actually-hook
///         `ConstitutionHook` alongside it.
///
/// @dev    Rule encodings:
///           MAX_LEVERAGE          : abi.encode(uint256 maxLeverageBps)
///           MAX_TRADE_SIZE        : abi.encode(uint256 maxAmount)
///           VENUE_BLACKLIST       : abi.encode(address[] blacklist)
///           NO_UNAUDITED_CONTRACTS: abi.encode(address[] whitelist)
///           SUBDELEGATION_BOUND   : abi.encode(uint256 maxChildAllowance)
///           CUSTOM                : ignored
///
///         Adapter wiring (Phase 5 Stream M):
///           When `Rule.adapter != address(0)`, the validator delegates
///           inner-calldata decoding to the adapter (see
///           IRuleAdapter.sol). This lets MAX_TRADE_SIZE / MAX_LEVERAGE
///           apply to real protocols (Drift v2 place_perp_order,
///           GMX v2 createOrder, Permit2 permitTransferFrom) instead of
///           only literal `ERC20.transfer` calls. The fall-back inline
///           decoder is preserved for the existing demo path.
///
///         F6 fix (Phase 5 Stream M):
///           The previous F6 patch (`require(msg.sender == userOp.sender)`)
///           broke real ERC-4337 because the EntryPoint, not the SCA,
///           calls `validateUserOp`. The new check is install-time
///           configurable: each agent registers the EntryPoint address
///           that drives them when they install the validator, and the
///           gate becomes `msg.sender == entryPoint OR msg.sender ==
///           userOp.sender`. The OR branch keeps direct unit-test calls
///           working; the AND-equivalent of EntryPoint-only ops is the
///           production path.
contract ConstitutionValidator is IValidator {
    /// @notice ERC-7579 execute(address,uint256,bytes) selector.
    bytes4 internal constant EXECUTE_SELECTOR = 0xb61d27f6;

    /// @notice ERC-20 transfer(address,uint256) selector — used by the
    ///         inline MAX_TRADE_SIZE fast path for rules with no adapter.
    bytes4 internal constant ERC20_TRANSFER_SELECTOR = 0xa9059cbb;

    /// @notice Canonical ERC-4337 v0.7 EntryPoint address. Reproduced
    ///         from eth-infinitism/account-abstraction; the contract is
    ///         deployed via deterministic CREATE2 on every chain that
    ///         honours the canonical factory. Used as the install-time
    ///         default when callers don't pass an explicit address.
    ///         Source: https://github.com/eth-infinitism/account-abstraction/releases/tag/v0.7.0
    address public constant DEFAULT_ENTRYPOINT_V07 =
        0x0000000071727De22E5E9d8BAf0edAc6f37da032;

    ConstitutionRegistry public immutable registry;

    /// @notice smart-account => constitution hash that gates it
    mapping(address => bytes32) public constitutionOf;

    /// @notice smart-account => the EntryPoint authorised to call
    ///         `validateUserOp` on its behalf. Set at install time;
    ///         falls back to `DEFAULT_ENTRYPOINT_V07` when the install
    ///         payload contains only the constitution hash (32 bytes).
    mapping(address => address) public entryPointOf;

    event ConstitutionInstalled(
        address indexed smartAccount, bytes32 constitutionHash, address entryPoint
    );
    event ConstitutionUninstalled(address indexed smartAccount);
    event ConstitutionViolation(address indexed agent, uint256 indexed ruleId, bytes reason);

    error InvalidInstallData();
    error UnknownConstitution();
    error NotInstalled();
    error AlreadyInstalled();
    error UnsupportedModuleType();
    error UnsupportedOuterSelector(bytes4 selector);

    /// @notice An adapter-requiring rule (MAX_LEVERAGE / SUBDELEGATION_BOUND)
    ///         reached the validator with `adapter == address(0)`.
    /// @dev    B16. The ConstitutionRegistry (the primary gate) rejects such
    ///         rules at `defineConstitution`, so a constitution stored under
    ///         a valid hash CANNOT contain one. If we ever observe it here it
    ///         is an invariant violation (registry bypassed / corrupted), not
    ///         a normal path — so we fail closed instead of silently
    ///         no-op'ing as the pre-B16 fail-open did.
    error MissingRequiredAdapter(uint8 kind);

    constructor(ConstitutionRegistry _registry) {
        registry = _registry;
    }

    // -----------------------------------------------------------------------
    // ERC-7579 module surface
    // -----------------------------------------------------------------------

    /// @notice Install the validator on the calling smart-account.
    /// @param  data Either:
    ///                - 32 bytes: abi-encoded `bytes32 constitutionHash`
    ///                            (EntryPoint defaults to v0.7 canonical).
    ///                - 64 bytes: abi-encoded `(bytes32 constitutionHash,
    ///                            address entryPoint)`.
    function onInstall(bytes calldata data) external override {
        bytes32 hash;
        address ep;
        if (data.length == 32) {
            hash = abi.decode(data, (bytes32));
            ep = DEFAULT_ENTRYPOINT_V07;
        } else if (data.length == 64) {
            (hash, ep) = abi.decode(data, (bytes32, address));
            // Zero entryPoint would unconditionally fail the F6 gate
            // for every real userOp. Reject install-time so the
            // operator catches the misconfiguration on install rather
            // than every userOp later.
            if (ep == address(0)) revert InvalidInstallData();
        } else {
            revert InvalidInstallData();
        }

        if (!registry.exists(hash)) revert UnknownConstitution();
        if (constitutionOf[msg.sender] != bytes32(0)) revert AlreadyInstalled();

        constitutionOf[msg.sender] = hash;
        entryPointOf[msg.sender] = ep;
        emit ConstitutionInstalled(msg.sender, hash, ep);
    }

    function onUninstall(bytes calldata /* data */) external override {
        if (constitutionOf[msg.sender] == bytes32(0)) revert NotInstalled();
        delete constitutionOf[msg.sender];
        delete entryPointOf[msg.sender];
        emit ConstitutionUninstalled(msg.sender);
    }

    function isModuleType(uint256 moduleTypeId) external pure override returns (bool) {
        return moduleTypeId == MODULE_TYPE_VALIDATOR;
    }

    function isInitialized(address smartAccount) external view override returns (bool) {
        return constitutionOf[smartAccount] != bytes32(0);
    }

    /// @notice Not in use; constitutional validation is for user-ops.
    function isValidSignatureWithSender(address, bytes32, bytes calldata)
        external
        pure
        override
        returns (bytes4)
    {
        return 0xffffffff;
    }

    // -----------------------------------------------------------------------
    // Core rule check
    // -----------------------------------------------------------------------

    /// @notice Run every rule on the agent's constitution against the
    ///         submitted user-op. Reverts on the first violation and
    ///         emits `ConstitutionViolation` so observers can correlate
    ///         the failed tx.
    function validateUserOp(PackedUserOperation calldata userOp, bytes32 /* userOpHash */)
        external
        override
        returns (uint256 validationData)
    {
        // F6 fix (Phase 5 Stream M). Accept the call ONLY from either
        //   (a) the EntryPoint the SCA registered at install time --
        //       the canonical ERC-4337 v0.7 production path, OR
        //   (b) the SCA itself -- the direct unit-test path.
        // Anyone else is rejected to preserve the reputation-griefing
        // protection that motivated the original F6 patch.
        bytes32 hash = constitutionOf[userOp.sender];
        if (hash == bytes32(0)) revert NotInstalled();

        address ep = entryPointOf[userOp.sender];
        require(
            msg.sender == ep || msg.sender == userOp.sender,
            "ConstitutionViolation:NOT_OWN_USEROP"
        );

        ConstitutionRegistry.Rule[] memory rules = registry.getConstitution(hash);

        // Decode the user-op's call: target / value / inner calldata.
        (address target, uint256 value, bytes memory innerData) =
            _decodeCall(userOp.callData);

        for (uint256 i = 0; i < rules.length; ++i) {
            ConstitutionRegistry.Rule memory r = rules[i];
            _enforce(userOp.sender, i, r, target, value, innerData);
        }

        return VALIDATION_SUCCESS;
    }

    // -----------------------------------------------------------------------
    // Rule dispatch
    // -----------------------------------------------------------------------

    function _enforce(
        address agent,
        uint256 ruleId,
        ConstitutionRegistry.Rule memory r,
        address target,
        uint256 value,
        bytes memory innerData
    ) internal {
        if (r.kind == 0) {
            _checkMaxLeverage(agent, ruleId, r, innerData);
        } else if (r.kind == 1) {
            _checkMaxTradeSize(agent, ruleId, r, value, innerData);
        } else if (r.kind == 2) {
            _checkVenueBlacklist(agent, ruleId, r.params, target);
        } else if (r.kind == 3) {
            _checkUnauditedContracts(agent, ruleId, r.params, target);
        } else if (r.kind == 4) {
            _checkSubdelegationBound(agent, ruleId, r, innerData);
        }
        // KIND_CUSTOM (255) and unknown kinds: no-op.
    }

    function _checkMaxLeverage(
        address agent,
        uint256 ruleId,
        ConstitutionRegistry.Rule memory r,
        bytes memory innerData
    ) internal {
        // MAX_LEVERAGE requires an adapter — pre-Phase-5, this rule
        // fired on a made-up selector (`setLeverage(uint256)`) that no
        // real protocol uses. Leverage is only derivable by an adapter
        // decoding real perp calldata (GmxV2PerpAdapter exposes
        // leverageBps; DriftPerpAdapter intentionally returns 0 and is
        // treated as not-applicable here).
        //
        // B16: the ConstitutionRegistry is the PRIMARY gate — it rejects
        // a MAX_LEVERAGE rule with `adapter == address(0)` at
        // `defineConstitution`, so this branch is unreachable for any
        // constitution stored under a valid hash. We keep it as
        // defense-in-depth and fail CLOSED (revert) rather than the
        // pre-B16 fail-open (`return`): a zero adapter arriving here means
        // the registry invariant was bypassed, not a normal path.
        if (r.adapter == address(0)) revert MissingRequiredAdapter(0);

        IRuleAdapter.Decoded memory dec = _tryDecode(r.adapter, innerData);
        // Adapter not applicable to this calldata? Skip.
        if (dec.leverageBps == 0) return;

        uint256 maxBps = abi.decode(r.params, (uint256));
        if (dec.leverageBps > maxBps) {
            emit ConstitutionViolation(
                agent, ruleId, abi.encode("MAX_LEVERAGE", maxBps, dec.leverageBps)
            );
            revert(_reason("MAX_LEVERAGE"));
        }
    }

    function _checkMaxTradeSize(
        address agent,
        uint256 ruleId,
        ConstitutionRegistry.Rule memory r,
        uint256 value,
        bytes memory innerData
    ) internal {
        uint256 max = abi.decode(r.params, (uint256));

        // Direct ETH/native USDC value transfer. Always checked even
        // when an adapter is wired -- a userOp with `value > 0` and
        // an unrelated inner selector still moves native funds.
        if (value > max) {
            emit ConstitutionViolation(
                agent, ruleId, abi.encode("MAX_TRADE_SIZE", max, value)
            );
            revert(_reason("MAX_TRADE_SIZE"));
        }

        // Adapter path. Lets MAX_TRADE_SIZE catch DEX / perp / permit
        // calls that bypass the inline `transfer(address,uint256)`
        // detection.
        if (r.adapter != address(0)) {
            IRuleAdapter.Decoded memory dec = _tryDecode(r.adapter, innerData);
            if (dec.sizeUsdc > max) {
                emit ConstitutionViolation(
                    agent, ruleId,
                    abi.encode("MAX_TRADE_SIZE_ADAPTER", max, dec.sizeUsdc)
                );
                revert(_reason("MAX_TRADE_SIZE"));
            }
        }

        // Inline fast path: ERC-20 transfer(address,uint256). Kept for
        // backward compatibility -- the demo path triggers here.
        if (innerData.length >= 68) {
            bytes4 sel = _selector(innerData);
            if (sel == ERC20_TRANSFER_SELECTOR) {
                uint256 amount = _decodeUint256At(innerData, 36);
                if (amount > max) {
                    emit ConstitutionViolation(
                        agent, ruleId,
                        abi.encode("MAX_TRADE_SIZE_ERC20", max, amount)
                    );
                    revert(_reason("MAX_TRADE_SIZE"));
                }
            }
        }
    }

    function _checkVenueBlacklist(
        address agent,
        uint256 ruleId,
        bytes memory params,
        address target
    ) internal {
        address[] memory blacklist = abi.decode(params, (address[]));
        for (uint256 i = 0; i < blacklist.length; ++i) {
            if (blacklist[i] == target) {
                emit ConstitutionViolation(
                    agent, ruleId, abi.encode("VENUE_BLACKLIST", target)
                );
                revert(_reason("VENUE_BLACKLIST"));
            }
        }
    }

    function _checkUnauditedContracts(
        address agent,
        uint256 ruleId,
        bytes memory params,
        address target
    ) internal {
        address[] memory whitelist = abi.decode(params, (address[]));
        if (whitelist.length == 0) return;

        for (uint256 i = 0; i < whitelist.length; ++i) {
            if (whitelist[i] == target) return;
        }
        emit ConstitutionViolation(
            agent, ruleId, abi.encode("NO_UNAUDITED_CONTRACTS", target)
        );
        revert(_reason("NO_UNAUDITED_CONTRACTS"));
    }

    /// @dev SUBDELEGATION_BOUND. The pre-Phase-5 implementation matched
    ///      a made-up `issueSessionKey(address,uint256)` selector. The
    ///      real EIP-7715 path is `wallet_requestExecutionPermissions`,
    ///      whose calldata-equivalent (when a permission is redeemed
    ///      on-chain through an ERC-7710-style delegation manager) is
    ///      the manager's `redeemDelegation(bytes context,...)`. Since
    ///      that context is opaque to us, we delegate to an adapter:
    ///      when an adapter is attached, we use the adapter's
    ///      `sizeUsdc` as the proposed child allowance and compare
    ///      against the rule param. Without an adapter, the rule is
    ///      not enforceable on real calldata -- we skip silently.
    function _checkSubdelegationBound(
        address agent,
        uint256 ruleId,
        ConstitutionRegistry.Rule memory r,
        bytes memory innerData
    ) internal {
        // B16: registry guarantees a non-zero adapter for SUBDELEGATION_BOUND
        // (see ConstitutionRegistry._adapterRequired). Defense-in-depth:
        // fail closed if the invariant is somehow violated.
        if (r.adapter == address(0)) revert MissingRequiredAdapter(4);

        IRuleAdapter.Decoded memory dec = _tryDecode(r.adapter, innerData);
        if (dec.sizeUsdc == 0) return;

        uint256 max = abi.decode(r.params, (uint256));
        if (dec.sizeUsdc > max) {
            emit ConstitutionViolation(
                agent, ruleId,
                abi.encode("SUBDELEGATION_BOUND", max, dec.sizeUsdc)
            );
            revert(_reason("SUBDELEGATION_BOUND"));
        }
    }

    // -----------------------------------------------------------------------
    // Adapter call (try / catch)
    // -----------------------------------------------------------------------

    /// @dev Call the adapter's `decode` in a try/catch so that a
    ///      "not applicable" revert (e.g. wrong selector for this
    ///      adapter) doesn't kill the whole validation. Truly
    ///      malformed inputs (`Malformed()`) still bubble — those
    ///      indicate a real attempt to forge a partial calldata.
    function _tryDecode(address adapter, bytes memory innerData)
        internal
        view
        returns (IRuleAdapter.Decoded memory dec)
    {
        try IRuleAdapter(adapter).decode(innerData) returns (
            IRuleAdapter.Decoded memory _dec
        ) {
            dec = _dec;
        } catch (bytes memory err) {
            // NotApplicable(bytes4) starts with selector
            // bytes4(keccak256("NotApplicable(bytes4)")) = 0xa70d717b.
            // Catching this is fine -- this adapter doesn't handle
            // this calldata. Any other error (Malformed, OOG, etc.)
            // we re-raise so the userOp fails closed.
            if (err.length >= 4) {
                bytes4 errSel;
                assembly {
                    errSel := mload(add(err, 32))
                }
                if (errSel == IRuleAdapter.NotApplicable.selector) {
                    // Return a default-empty Decoded -- the caller
                    // sees `sizeUsdc == 0 && leverageBps == 0` and
                    // treats this rule-call combo as not applicable.
                    return dec;
                }
            }
            // Re-raise the original revert data.
            assembly {
                revert(add(err, 32), mload(err))
            }
        }
    }

    // -----------------------------------------------------------------------
    // Decoding helpers
    // -----------------------------------------------------------------------

    /// @dev Decode `callData` as an ERC-7579 ``execute(address,uint256,bytes)``
    ///      payload. F4 hardening — we fail closed: any callData whose
    ///      outer selector is not ``EXECUTE_SELECTOR`` reverts with
    ///      ``UnsupportedOuterSelector``. Without this, the previous
    ///      fall-through returned ``(address(0), 0, callData)`` and
    ///      silently passed batched executes, raw selectors, and even
    ///      empty calldata through the validator.
    function _decodeCall(bytes calldata callData)
        internal
        pure
        returns (address target, uint256 value, bytes memory innerData)
    {
        if (callData.length < 4) {
            revert UnsupportedOuterSelector(bytes4(0));
        }
        bytes4 sel;
        assembly {
            sel := calldataload(callData.offset)
        }
        if (sel == EXECUTE_SELECTOR) {
            if (callData.length < 4 + 32 * 3) {
                revert UnsupportedOuterSelector(sel);
            }
            bytes memory body = callData[4:];
            (target, value, innerData) = abi.decode(body, (address, uint256, bytes));
            return (target, value, innerData);
        }
        revert UnsupportedOuterSelector(sel);
    }

    function _selector(bytes memory data) internal pure returns (bytes4 sel) {
        assembly {
            sel := mload(add(data, 32))
        }
    }

    function _decodeUint256At(bytes memory data, uint256 offset)
        internal
        pure
        returns (uint256 v)
    {
        assembly {
            v := mload(add(add(data, 32), offset))
        }
    }

    function _reason(string memory tag) internal pure returns (string memory) {
        return string(abi.encodePacked("ConstitutionViolation:", tag));
    }
}
