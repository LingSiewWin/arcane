// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {
    IExecutor,
    IERC7579Account,
    MODULE_TYPE_EXECUTOR
} from "./interfaces/IERC7579.sol";
import {ConstitutionRegistry} from "./ConstitutionRegistry.sol";
import {IRuleAdapter} from "./adapters/IRuleAdapter.sol";

/// @title  ConstitutionExecutor
/// @notice ERC-7579 *executor* module (MODULE_TYPE_EXECUTOR = 2).
///
///         An executor is a module that initiates calls FROM the smart
///         account, via `account.executeFromExecutor(mode, execData)`.
///         Executors are powerful: they bypass the validator entirely
///         (validators only gate ERC-4337 userOps, not internal
///         executor-initiated calls). The Phase-4 benchmark
///         (`docs/benchmark_realworld.md` §4) called out this gap:
///         "if a malicious executor module is installed, it can call
///         `execute()` on the account *without* triggering our
///         validator". This contract closes the gap by:
///
///         1. Being the ONLY executor the account is supposed to use
///            for constitution-governed flows. The owner/SCA must
///            uninstall any other executor at install time, or
///            external policy guarantees only this executor is
///            installed.
///         2. Re-running the same rule check the validator runs,
///            BEFORE issuing the executeFromExecutor call. This means
///            even a self-initiated action (Bob calling himself
///            through this executor) is subject to the constitution.
///         3. Pairing with `ConstitutionHook` (which runs on every
///            executeFromExecutor regardless of which executor issued
///            it) as defence in depth — a malicious executor that
///            DIDN'T pre-check would still trigger the hook's
///            preCheck/postCheck.
///
/// @dev    Usage:
///           ConstitutionExecutor(addr).execute(target, value, data);
///         The executor MUST be installed on `account` as
///         MODULE_TYPE_EXECUTOR; only then can it call back into
///         `account.executeFromExecutor`. The account address is
///         registered at install time -- one executor instance maps
///         to one account.
contract ConstitutionExecutor is IExecutor {
    bytes4 internal constant ERC20_TRANSFER_SELECTOR = 0xa9059cbb;

    /// @notice ERC-7579 execution mode for a single call:
    ///         callType=0x00 (single), execType=0x00 (revert), modeSelector=0,
    ///         modePayload=0. Packed into bytes32 as
    ///         0x0000000000000000000000000000000000000000000000000000000000000000.
    bytes32 internal constant SINGLE_CALL_MODE = bytes32(0);

    ConstitutionRegistry public immutable registry;

    /// @notice executor instance => account it manages (set at install).
    ///         A single executor contract may serve many accounts.
    mapping(address => bytes32) public constitutionOf;

    event ConstitutionExecutorInstalled(address indexed account, bytes32 constitutionHash);
    event ConstitutionExecutorUninstalled(address indexed account);
    event ConstitutionViolation(address indexed agent, uint256 indexed ruleId, bytes reason);
    event ExecutedFromExecutor(address indexed account, address target, uint256 value);

    error InvalidInstallData();
    error UnknownConstitution();
    error NotInstalled();
    error AlreadyInstalled();
    error NotAuthorized();

    constructor(ConstitutionRegistry _registry) {
        registry = _registry;
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
        emit ConstitutionExecutorInstalled(msg.sender, hash);
    }

    function onUninstall(bytes calldata /* data */) external override {
        if (constitutionOf[msg.sender] == bytes32(0)) revert NotInstalled();
        delete constitutionOf[msg.sender];
        emit ConstitutionExecutorUninstalled(msg.sender);
    }

    function isModuleType(uint256 moduleTypeId) external pure override returns (bool) {
        return moduleTypeId == MODULE_TYPE_EXECUTOR;
    }

    function isInitialized(address smartAccount) external view override returns (bool) {
        return constitutionOf[smartAccount] != bytes32(0);
    }

    // -----------------------------------------------------------------------
    // Executor API (the constitution-governed callback into the account)
    // -----------------------------------------------------------------------

    /// @notice Issue a call from `account` to `target` with `value` /
    ///         `data`. The caller MUST be `account` itself (typical
    ///         use: the agent's owner EOA proxies through here) or a
    ///         pre-authorised operator -- enforced by the account's
    ///         own permissioning. The constitution governs the
    ///         resulting call.
    /// @param  account The smart account to act through.
    /// @param  target  Final call target.
    /// @param  value   Native value to forward.
    /// @param  data    Inner calldata.
    /// @return ret     Return data from the inner call.
    function execute(address account, address target, uint256 value, bytes calldata data)
        external
        returns (bytes[] memory ret)
    {
        bytes32 hash = constitutionOf[account];
        if (hash == bytes32(0)) revert NotInstalled();

        // Authorisation: only the account itself or its owner may
        // invoke this executor. We accept either:
        //   (a) msg.sender == account            (account self-calls)
        //   (b) msg.sender is the EOA-owner --
        //       there is no canonical "owner" view on a vanilla SCA,
        //       so we delegate the check to the account: if the
        //       account refuses `executeFromExecutor`, the call below
        //       reverts and the require is moot.
        // The require below guards against the third party calling
        // execute() against an account that has no relationship with
        // them; the account's own permissioning would catch this
        // anyway, but failing fast here is better UX.
        if (msg.sender != account) revert NotAuthorized();

        // Pre-call constitution check (intent-level).
        ConstitutionRegistry.Rule[] memory rules = registry.getConstitution(hash);
        for (uint256 i = 0; i < rules.length; ++i) {
            ConstitutionRegistry.Rule memory r = rules[i];
            _enforce(account, i, r, target, value, data);
        }

        emit ExecutedFromExecutor(account, target, value);

        // Build the ERC-7579 single-call execData: packed (target | value | data).
        // Layout: address (20 bytes) || uint256 value (32 bytes) || data.
        bytes memory execData = abi.encodePacked(target, value, data);
        ret = IERC7579Account(account).executeFromExecutor(SINGLE_CALL_MODE, execData);
    }

    // -----------------------------------------------------------------------
    // Rule check (shared shape with validator / hook; replicated for
    // self-containment per Phase-5 audit guidance)
    // -----------------------------------------------------------------------

    function _enforce(
        address agent,
        uint256 ruleId,
        ConstitutionRegistry.Rule memory r,
        address target,
        uint256 value,
        bytes memory innerData
    ) internal {
        if (r.kind == 1) {
            _checkMaxTradeSize(agent, ruleId, r, value, innerData);
        } else if (r.kind == 2) {
            _checkVenueBlacklist(agent, ruleId, r.params, target);
        } else if (r.kind == 3) {
            _checkUnauditedContracts(agent, ruleId, r.params, target);
        } else if (r.kind == 0) {
            if (r.adapter != address(0)) {
                _checkMaxLeverage(agent, ruleId, r, innerData);
            }
        } else if (r.kind == 4) {
            if (r.adapter != address(0)) {
                _checkSubdelegationBound(agent, ruleId, r, innerData);
            }
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
        if (value > max) {
            emit ConstitutionViolation(
                agent, ruleId, abi.encode("MAX_TRADE_SIZE", max, value)
            );
            revert("ConstitutionViolation:MAX_TRADE_SIZE");
        }

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

        if (innerData.length >= 68) {
            bytes4 sel;
            assembly {
                sel := mload(add(innerData, 32))
            }
            if (sel == ERC20_TRANSFER_SELECTOR) {
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
}
