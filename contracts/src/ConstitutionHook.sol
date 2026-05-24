// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {
    IHook,
    MODULE_TYPE_HOOK
} from "./interfaces/IERC7579.sol";
import {ConstitutionRegistry} from "./ConstitutionRegistry.sol";
import {IRuleAdapter} from "./adapters/IRuleAdapter.sol";

/// @title  ConstitutionHook
/// @notice ERC-7579 *hook* module (MODULE_TYPE_HOOK = 4) implementing the
///         canonical `preCheck(address,uint256,bytes)` / `postCheck(bytes)`
///         lifecycle. Runs on EVERY call routed through the account,
///         including `executeFromExecutor` calls initiated by other
///         installed executor modules — closes the validator-bypass
///         gap identified in `docs/benchmark_realworld.md` §4 and
///         `docs/audit_phase4_review.md` finding B7.
///
///         The hook complements (does NOT replace) ConstitutionValidator:
///           - Validator gates the *intent* (the userOp's callData) at
///             ERC-4337 validation time.
///           - Hook gates the *outcome* (each individual `call` the
///             account makes) — and runs even when the call is
///             initiated by an executor module rather than a userOp.
///
///         What `postCheck` enforces. The hook stashes the account's
///         pre-call USDC balance in `hookData` returned from
///         `preCheck`. After the call lands, `postCheck` re-reads the
///         balance and reverts if the delta exceeds the constitution's
///         MAX_TRADE_SIZE rule — catching real outcome violations the
///         validator could not (e.g., the inner call drained more than
///         the intent declared, via reentrancy or unexpected token
///         flows).
///
/// @dev    Token-balance tracking. The hook reads the USDC balance from
///         a constructor-provided token address. This is the canonical
///         deployment posture: each constitution applies to a specific
///         settlement token (USDC on Arc, USDC on Base, etc). The
///         token address is set at construction so the hook is
///         self-contained and immutable.
contract ConstitutionHook is IHook {
    /// @notice ERC-7579 execute(address,uint256,bytes) selector — only
    ///         supported outer selector for now; matches the validator.
    bytes4 internal constant EXECUTE_SELECTOR = 0xb61d27f6;

    /// @notice ERC-7579 executeFromExecutor — when an installed executor
    ///         module calls the account, this is the selector the
    ///         account routes through. The hook sees `msgSender` equal
    ///         to the executor module address. We treat these calls
    ///         identically to validator-approved userOp executes for
    ///         the purposes of rule enforcement.
    bytes4 internal constant EXECUTE_FROM_EXECUTOR_SELECTOR = 0xd691c964;

    /// @notice Minimal ERC-20 balance-of interface.
    bytes4 internal constant ERC20_BALANCE_OF_SELECTOR = 0x70a08231;

    /// @notice ERC-20 token whose balance we track for outcome
    ///         enforcement. Set at construction; immutable.
    address public immutable token;

    ConstitutionRegistry public immutable registry;

    /// @notice smart-account => constitution hash that gates it. Mirrors
    ///         the validator's mapping but independent (a smart account
    ///         can install the validator without the hook and vice
    ///         versa).
    mapping(address => bytes32) public constitutionOf;

    event ConstitutionHookInstalled(address indexed smartAccount, bytes32 constitutionHash);
    event ConstitutionHookUninstalled(address indexed smartAccount);
    event PreCheck(address indexed account, address msgSender, uint256 msgValue, bytes4 outerSel);
    event PostCheck(address indexed account, int256 deltaUsdc);
    event ConstitutionViolation(address indexed agent, uint256 indexed ruleId, bytes reason);

    error InvalidInstallData();
    error UnknownConstitution();
    error NotInstalled();
    error AlreadyInstalled();
    error UnsupportedOuterSelector(bytes4 selector);
    error BalanceQueryFailed();

    constructor(ConstitutionRegistry _registry, address _token) {
        registry = _registry;
        token = _token;
    }

    // -----------------------------------------------------------------------
    // ERC-7579 module surface
    // -----------------------------------------------------------------------

    function onInstall(bytes calldata data) external override {
        if (data.length != 32) revert InvalidInstallData();
        bytes32 hash = abi.decode(data, (bytes32));
        if (!registry.exists(hash)) revert UnknownConstitution();
        if (constitutionOf[msg.sender] != bytes32(0)) revert AlreadyInstalled();

        constitutionOf[msg.sender] = hash;
        emit ConstitutionHookInstalled(msg.sender, hash);
    }

    function onUninstall(bytes calldata /* data */) external override {
        if (constitutionOf[msg.sender] == bytes32(0)) revert NotInstalled();
        delete constitutionOf[msg.sender];
        emit ConstitutionHookUninstalled(msg.sender);
    }

    function isModuleType(uint256 moduleTypeId) external pure override returns (bool) {
        return moduleTypeId == MODULE_TYPE_HOOK;
    }

    function isInitialized(address smartAccount) external view override returns (bool) {
        return constitutionOf[smartAccount] != bytes32(0);
    }

    // -----------------------------------------------------------------------
    // Hook surface (the real type-4 interface)
    // -----------------------------------------------------------------------

    /// @notice Runs BEFORE every call the smart account makes. The
    ///         account is `msg.sender` of this function (ERC-7579
    ///         dispatch). `msgSender` is whoever caused the account
    ///         to make the outer call -- the EntryPoint (for a
    ///         validator-approved userOp), an executor module (for
    ///         `executeFromExecutor`), or a fallback caller.
    /// @return hookData abi-encoded snapshot the postCheck will need.
    ///         We pack:
    ///           (uint256 maxTradeSize,
    ///            uint256 preBalance,
    ///            bool    hasMaxTradeSizeRule)
    ///         If the agent has no MAX_TRADE_SIZE rule, `hasMaxTradeSizeRule`
    ///         is false and `postCheck` no-ops.
    function preCheck(address msgSender, uint256 msgValue, bytes calldata msgData)
        external
        override
        returns (bytes memory hookData)
    {
        address account = msg.sender;
        bytes32 hash = constitutionOf[account];
        if (hash == bytes32(0)) revert NotInstalled();

        // Decode the outer selector for diagnostics and rule routing.
        if (msgData.length < 4) revert UnsupportedOuterSelector(bytes4(0));
        bytes4 outerSel;
        assembly {
            outerSel := calldataload(msgData.offset)
        }
        if (
            outerSel != EXECUTE_SELECTOR &&
            outerSel != EXECUTE_FROM_EXECUTOR_SELECTOR
        ) {
            revert UnsupportedOuterSelector(outerSel);
        }

        emit PreCheck(account, msgSender, msgValue, outerSel);

        // Walk rules. We enforce intent-level rules here (selectors,
        // blacklists, sizes that the calldata declares); outcome-only
        // checks (balance delta) are deferred to postCheck.
        ConstitutionRegistry.Rule[] memory rules = registry.getConstitution(hash);
        (address target, uint256 value, bytes memory innerData) =
            _decodeOuter(msgData, outerSel);

        uint256 maxTradeSize;
        bool hasMaxTradeSizeRule;

        for (uint256 i = 0; i < rules.length; ++i) {
            ConstitutionRegistry.Rule memory r = rules[i];
            if (r.kind == 1) {
                // MAX_TRADE_SIZE -- record for postCheck outcome
                // enforcement AND enforce the inline / adapter check
                // now (intent-level).
                maxTradeSize = abi.decode(r.params, (uint256));
                hasMaxTradeSizeRule = true;
                _enforceMaxTradeSize(account, i, r, value, innerData, maxTradeSize);
            } else if (r.kind == 2) {
                _checkVenueBlacklist(account, i, r.params, target);
            } else if (r.kind == 3) {
                _checkUnauditedContracts(account, i, r.params, target);
            } else if (r.kind == 0) {
                // MAX_LEVERAGE via adapter (same logic as validator).
                if (r.adapter != address(0)) {
                    _checkMaxLeverage(account, i, r, innerData);
                }
            } else if (r.kind == 4) {
                if (r.adapter != address(0)) {
                    _checkSubdelegationBound(account, i, r, innerData);
                }
            }
        }

        uint256 preBalance = _balanceOf(account);
        hookData = abi.encode(maxTradeSize, preBalance, hasMaxTradeSizeRule);
    }

    /// @notice Runs AFTER the outer call. The account is `msg.sender`
    ///         again. If MAX_TRADE_SIZE was active and the post-call
    ///         balance dropped by more than the rule allows, revert.
    function postCheck(bytes calldata hookData) external override {
        address account = msg.sender;
        if (constitutionOf[account] == bytes32(0)) revert NotInstalled();

        (uint256 maxTradeSize, uint256 preBalance, bool hasRule) =
            abi.decode(hookData, (uint256, uint256, bool));

        uint256 postBalance = _balanceOf(account);
        // int256 delta is signed: postBalance - preBalance.
        int256 delta = int256(postBalance) - int256(preBalance);
        emit PostCheck(account, delta);

        if (!hasRule) return;

        // Outcome enforcement: if the account paid out more than the
        // rule allows, revert. We compare the magnitude of the
        // outflow (preBalance - postBalance) against maxTradeSize.
        if (postBalance < preBalance) {
            uint256 outflow = preBalance - postBalance;
            if (outflow > maxTradeSize) {
                emit ConstitutionViolation(
                    account, 1, abi.encode("MAX_TRADE_SIZE_POSTCHECK", maxTradeSize, outflow)
                );
                revert("ConstitutionViolation:POSTCHECK_OUTFLOW");
            }
        }
    }

    // -----------------------------------------------------------------------
    // Rule helpers (shared with validator; reproduced here so the hook
    // remains self-contained -- the auditor flagged shared-state coupling
    // between validator and hook as a smell).
    // -----------------------------------------------------------------------

    function _enforceMaxTradeSize(
        address agent,
        uint256 ruleId,
        ConstitutionRegistry.Rule memory r,
        uint256 value,
        bytes memory innerData,
        uint256 max
    ) internal {
        if (value > max) {
            emit ConstitutionViolation(
                agent, ruleId, abi.encode("MAX_TRADE_SIZE", max, value)
            );
            revert("ConstitutionViolation:MAX_TRADE_SIZE");
        }

        // Adapter path.
        if (r.adapter != address(0)) {
            IRuleAdapter.Decoded memory dec = _tryDecode(r.adapter, innerData);
            if (dec.sizeUsdc > max) {
                emit ConstitutionViolation(
                    agent, ruleId,
                    abi.encode("MAX_TRADE_SIZE_ADAPTER", max, dec.sizeUsdc)
                );
                revert("ConstitutionViolation:MAX_TRADE_SIZE");
            }
        }

        // Inline ERC-20 transfer fast path.
        if (innerData.length >= 68) {
            bytes4 sel;
            assembly {
                sel := mload(add(innerData, 32))
            }
            if (sel == 0xa9059cbb) {
                uint256 amount;
                assembly {
                    amount := mload(add(innerData, 68))
                }
                if (amount > max) {
                    emit ConstitutionViolation(
                        agent, ruleId,
                        abi.encode("MAX_TRADE_SIZE_ERC20", max, amount)
                    );
                    revert("ConstitutionViolation:MAX_TRADE_SIZE");
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
                revert("ConstitutionViolation:VENUE_BLACKLIST");
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
        revert("ConstitutionViolation:NO_UNAUDITED_CONTRACTS");
    }

    function _checkMaxLeverage(
        address agent,
        uint256 ruleId,
        ConstitutionRegistry.Rule memory r,
        bytes memory innerData
    ) internal {
        IRuleAdapter.Decoded memory dec = _tryDecode(r.adapter, innerData);
        if (dec.leverageBps == 0) return;

        uint256 maxBps = abi.decode(r.params, (uint256));
        if (dec.leverageBps > maxBps) {
            emit ConstitutionViolation(
                agent, ruleId, abi.encode("MAX_LEVERAGE", maxBps, dec.leverageBps)
            );
            revert("ConstitutionViolation:MAX_LEVERAGE");
        }
    }

    function _checkSubdelegationBound(
        address agent,
        uint256 ruleId,
        ConstitutionRegistry.Rule memory r,
        bytes memory innerData
    ) internal {
        IRuleAdapter.Decoded memory dec = _tryDecode(r.adapter, innerData);
        if (dec.sizeUsdc == 0) return;

        uint256 max = abi.decode(r.params, (uint256));
        if (dec.sizeUsdc > max) {
            emit ConstitutionViolation(
                agent, ruleId,
                abi.encode("SUBDELEGATION_BOUND", max, dec.sizeUsdc)
            );
            revert("ConstitutionViolation:SUBDELEGATION_BOUND");
        }
    }

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
            if (err.length >= 4) {
                bytes4 errSel;
                assembly {
                    errSel := mload(add(err, 32))
                }
                if (errSel == IRuleAdapter.NotApplicable.selector) {
                    return dec;
                }
            }
            assembly {
                revert(add(err, 32), mload(err))
            }
        }
    }

    function _decodeOuter(bytes calldata msgData, bytes4 outerSel)
        internal
        pure
        returns (address target, uint256 value, bytes memory innerData)
    {
        if (outerSel == EXECUTE_SELECTOR) {
            if (msgData.length < 4 + 32 * 3) {
                revert UnsupportedOuterSelector(outerSel);
            }
            (target, value, innerData) = abi.decode(msgData[4:], (address, uint256, bytes));
        } else if (outerSel == EXECUTE_FROM_EXECUTOR_SELECTOR) {
            // ERC-7579 executeFromExecutor(bytes32 mode, bytes execData).
            // For single-call mode, execData = abi.encodePacked(target, value, data)
            // PACKED -- 20 bytes target | 32 bytes value | rest data.
            // We don't dynamically dispatch on mode here; we extract
            // the canonical single-call shape and bail if the
            // packing doesn't fit.
            if (msgData.length < 4 + 32 * 4) {
                revert UnsupportedOuterSelector(outerSel);
            }
            (, bytes memory execData) = abi.decode(msgData[4:], (bytes32, bytes));
            if (execData.length < 20 + 32) {
                revert UnsupportedOuterSelector(outerSel);
            }
            assembly {
                // target = first 20 bytes
                target := shr(96, mload(add(execData, 32)))
                // value = next 32 bytes
                value := mload(add(execData, 52))
            }
            // innerData = execData[52:]
            uint256 innerLen = execData.length - 52;
            innerData = new bytes(innerLen);
            for (uint256 i = 0; i < innerLen; ++i) {
                innerData[i] = execData[52 + i];
            }
        } else {
            revert UnsupportedOuterSelector(outerSel);
        }
    }

    /// @dev Static-call the token's `balanceOf(address)`. Reverts if the call
    ///      fails or returns malformed data. We must NOT swallow a failure as a
    ///      zero balance: a real ERC-20 returns 32 bytes even for a zero
    ///      balance, so the only way to reach here with `!ok`/short data is a
    ///      broken/non-ERC-20 `token`. Treating that as 0 would set preBalance=0
    ///      and silently bypass the MAX_TRADE_SIZE outcome check in postCheck
    ///      (outflow = preBalance - postBalance can never trigger). Fail loud.
    function _balanceOf(address account) internal view returns (uint256 bal) {
        (bool ok, bytes memory ret) = token.staticcall(
            abi.encodeWithSelector(ERC20_BALANCE_OF_SELECTOR, account)
        );
        if (!ok || ret.length < 32) revert BalanceQueryFailed();
        bal = abi.decode(ret, (uint256));
    }
}
