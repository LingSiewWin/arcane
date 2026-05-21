// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {
    IValidator,
    PackedUserOperation,
    MODULE_TYPE_VALIDATOR,
    VALIDATION_SUCCESS
} from "./interfaces/IERC7579.sol";
import {ConstitutionRegistry} from "./ConstitutionRegistry.sol";

/// @title  ConstitutionHook
/// @notice ERC-7579 validator module that re-checks every user operation
///         against the agent's published constitution and reverts on
///         violation. Built minimally for hackathon scope:
///           - we decode the user-op's primary call (single execution, not
///             batched executes) by parsing `userOp.callData` as
///             `execute(address target, uint256 value, bytes data)` and
///             walking each rule.
///           - we also support a bare top-level call where `callData` is a
///             direct selector against the destination (for testability).
/// @dev    Rule encodings:
///           MAX_LEVERAGE          : abi.encode(uint256 maxLeverageBps)
///           MAX_TRADE_SIZE        : abi.encode(uint256 maxAmount)
///           VENUE_BLACKLIST       : abi.encode(address[] blacklist)
///           NO_UNAUDITED_CONTRACTS: abi.encode(address[] whitelist)
///           SUBDELEGATION_BOUND   : abi.encode(uint256 maxChildBudget)
///           CUSTOM                : ignored
contract ConstitutionHook is IValidator {
    // Selectors we recognise in the call payload.
    // execute(address,uint256,bytes) - ERC-7579 / smart-account convention.
    bytes4 internal constant EXECUTE_SELECTOR = 0xb61d27f6;
    // setLeverage(uint256) - signal for MAX_LEVERAGE rules.
    bytes4 internal constant SET_LEVERAGE_SELECTOR = 0x79575b23;
    // transfer(address,uint256) - ERC-20 transfer; treat second arg as amount.
    bytes4 internal constant ERC20_TRANSFER_SELECTOR = 0xa9059cbb;
    // issueSessionKey(address,uint256) - subdelegation signal.
    //   keccak256("issueSessionKey(address,uint256)")[:4]
    bytes4 internal constant ISSUE_SESSION_KEY_SELECTOR = 0x7873af1d;

    ConstitutionRegistry public immutable registry;

    /// @notice smart-account => constitution hash that gates it
    mapping(address => bytes32) public constitutionOf;

    event ConstitutionInstalled(address indexed smartAccount, bytes32 constitutionHash);
    event ConstitutionUninstalled(address indexed smartAccount);
    event ConstitutionViolation(address indexed agent, uint256 indexed ruleId, bytes reason);

    error InvalidInstallData();
    error UnknownConstitution();
    error NotInstalled();
    error AlreadyInstalled();
    error UnsupportedModuleType();
    /// @notice The user-op `callData` did not start with a recognised
    ///         outer selector. The hook now fails closed: if we can't
    ///         decode the call we cannot enforce rules against it, so we
    ///         reject the user-op rather than passing it through with a
    ///         zeroed target/value.
    error UnsupportedOuterSelector(bytes4 selector);

    constructor(ConstitutionRegistry _registry) {
        registry = _registry;
    }

    // -----------------------------------------------------------------------
    // ERC-7579 module surface
    // -----------------------------------------------------------------------

    /// @notice Install the hook on the calling smart-account.
    /// @param  data abi-encoded bytes32 constitutionHash. The hash must
    ///              already exist in the registry.
    function onInstall(bytes calldata data) external override {
        if (data.length != 32) revert InvalidInstallData();
        bytes32 hash = abi.decode(data, (bytes32));
        if (!registry.exists(hash)) revert UnknownConstitution();
        if (constitutionOf[msg.sender] != bytes32(0)) revert AlreadyInstalled();

        constitutionOf[msg.sender] = hash;
        emit ConstitutionInstalled(msg.sender, hash);
    }

    function onUninstall(bytes calldata /* data */) external override {
        if (constitutionOf[msg.sender] == bytes32(0)) revert NotInstalled();
        delete constitutionOf[msg.sender];
        emit ConstitutionUninstalled(msg.sender);
    }

    function isModuleType(uint256 moduleTypeId) external pure override returns (bool) {
        return moduleTypeId == MODULE_TYPE_VALIDATOR;
    }

    function isInitialized(address smartAccount) external view override returns (bool) {
        return constitutionOf[smartAccount] != bytes32(0);
    }

    /// @notice Not in use; constitutional hook is for user-ops, not sigs.
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
    ///         submitted user-op. Reverts on the first violation and emits
    ///         `ConstitutionViolation` so observers can correlate the failed
    ///         tx.
    function validateUserOp(PackedUserOperation calldata userOp, bytes32 /* userOpHash */)
        external
        override
        returns (uint256 validationData)
    {
        // F6 — caller gate. Only the smart account named in
        // ``userOp.sender`` may submit a validation request for itself.
        // Without this, any address can pass a victim's address as
        // ``userOp.sender`` and emit a ``ConstitutionViolation`` event
        // under the victim — a reputation-griefing primitive.
        require(msg.sender == userOp.sender, "ConstitutionViolation:NOT_OWN_USEROP");

        bytes32 hash = constitutionOf[userOp.sender];
        if (hash == bytes32(0)) revert NotInstalled();

        ConstitutionRegistry.Rule[] memory rules = registry.getConstitution(hash);

        // Decode the user-op's call: target / value / inner calldata.
        (address target, uint256 value, bytes memory innerData) = _decodeCall(userOp.callData);

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
            _checkMaxLeverage(agent, ruleId, r.params, innerData);
        } else if (r.kind == 1) {
            _checkMaxTradeSize(agent, ruleId, r.params, value, innerData);
        } else if (r.kind == 2) {
            _checkVenueBlacklist(agent, ruleId, r.params, target);
        } else if (r.kind == 3) {
            _checkUnauditedContracts(agent, ruleId, r.params, target);
        } else if (r.kind == 4) {
            _checkSubdelegationBound(agent, ruleId, r.params, innerData);
        }
        // KIND_CUSTOM (255) and unknown kinds: no-op.
    }

    function _checkMaxLeverage(
        address agent,
        uint256 ruleId,
        bytes memory params,
        bytes memory innerData
    ) internal {
        // Only act when the call is setLeverage(uint256).
        if (innerData.length < 36) return;
        bytes4 sel = _selector(innerData);
        if (sel != SET_LEVERAGE_SELECTOR) return;

        uint256 max = abi.decode(params, (uint256));
        uint256 requested = _decodeUint256At(innerData, 4);
        if (requested > max) {
            emit ConstitutionViolation(agent, ruleId, abi.encode("MAX_LEVERAGE", max, requested));
            revert(_reason("MAX_LEVERAGE"));
        }
    }

    function _checkMaxTradeSize(
        address agent,
        uint256 ruleId,
        bytes memory params,
        uint256 value,
        bytes memory innerData
    ) internal {
        uint256 max = abi.decode(params, (uint256));

        // Direct ETH/native USDC value transfer.
        if (value > max) {
            emit ConstitutionViolation(agent, ruleId, abi.encode("MAX_TRADE_SIZE", max, value));
            revert(_reason("MAX_TRADE_SIZE"));
        }

        // ERC-20 transfer payload? Peek at the amount argument.
        if (innerData.length >= 68) {
            bytes4 sel = _selector(innerData);
            if (sel == ERC20_TRANSFER_SELECTOR) {
                uint256 amount = _decodeUint256At(innerData, 36); // skip selector + address
                if (amount > max) {
                    emit ConstitutionViolation(
                        agent, ruleId, abi.encode("MAX_TRADE_SIZE_ERC20", max, amount)
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
                emit ConstitutionViolation(agent, ruleId, abi.encode("VENUE_BLACKLIST", target));
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
        // Empty whitelist == feature disabled.
        if (whitelist.length == 0) return;

        for (uint256 i = 0; i < whitelist.length; ++i) {
            if (whitelist[i] == target) return;
        }
        emit ConstitutionViolation(agent, ruleId, abi.encode("NO_UNAUDITED_CONTRACTS", target));
        revert(_reason("NO_UNAUDITED_CONTRACTS"));
    }

    function _checkSubdelegationBound(
        address agent,
        uint256 ruleId,
        bytes memory params,
        bytes memory innerData
    ) internal {
        if (innerData.length < 68) return;
        bytes4 sel = _selector(innerData);
        if (sel != ISSUE_SESSION_KEY_SELECTOR) return;

        uint256 max = abi.decode(params, (uint256));
        uint256 childBudget = _decodeUint256At(innerData, 36); // skip selector + child addr
        if (childBudget > max) {
            emit ConstitutionViolation(
                agent, ruleId, abi.encode("SUBDELEGATION_BOUND", max, childBudget)
            );
            revert(_reason("SUBDELEGATION_BOUND"));
        }
    }

    // -----------------------------------------------------------------------
    // Decoding helpers
    // -----------------------------------------------------------------------

    /// @dev Decode `callData` as an ERC-7579 ``execute(address,uint256,bytes)``
    ///      payload. F4 hardening — we now fail closed: any callData whose
    ///      outer selector is not ``EXECUTE_SELECTOR`` reverts with
    ///      ``UnsupportedOuterSelector``. Without this, the previous
    ///      fall-through returned ``(address(0), 0, callData)`` and
    ///      silently passed batched executes, raw selectors, and even
    ///      empty calldata through the hook — letting rules that key on
    ///      ``target`` (blacklist, whitelist) be bypassed entirely.
    ///
    ///      Future slices that need to support ``executeBatch`` or other
    ///      account standards must add their own branches here and
    ///      ship matching test coverage; the default remains REVERT.
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
