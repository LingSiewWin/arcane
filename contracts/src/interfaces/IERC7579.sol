// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

/// @notice ERC-7579 module-type surface used by the Constitution module suite.
/// @dev    We intentionally do not depend on `@rhinestone/modulekit`; this file
///         reproduces the four canonical ERC-7579 module interfaces
///         (validator, executor, fallback, hook) with the exact signatures
///         expected by Rhinestone-style ModuleKit accounts, plus the ERC-4337
///         v0.7 `PackedUserOperation` layout used by EntryPoint.
///
///         Sources:
///         - ERC-7579 (https://eips.ethereum.org/EIPS/eip-7579) module taxonomy
///         - rhinestonewtf/modulekit `ERC7579ValidatorBase` / `ERC7579ExecutorBase` /
///           `ERC7579FallbackBase` / `ERC7579HookBase` for the canonical
///           function signatures.
///         - eth-infinitism/account-abstraction v0.7 EntryPoint for
///           `PackedUserOperation`.

// ---------------------------------------------------------------------------
// ERC-4337 v0.7 packed user-op layout. Mirrors EntryPoint's struct.
// ---------------------------------------------------------------------------
struct PackedUserOperation {
    address sender;
    uint256 nonce;
    bytes initCode;
    bytes callData;
    bytes32 accountGasLimits; // verificationGasLimit (16 bytes) | callGasLimit (16 bytes)
    uint256 preVerificationGas;
    bytes32 gasFees;          // maxPriorityFeePerGas (16 bytes) | maxFeePerGas (16 bytes)
    bytes paymasterAndData;
    bytes signature;
}

// ---------------------------------------------------------------------------
// ERC-7579 module-type identifiers. Stable across all 7579 accounts.
// ---------------------------------------------------------------------------
uint256 constant MODULE_TYPE_VALIDATOR = 1;
uint256 constant MODULE_TYPE_EXECUTOR  = 2;
uint256 constant MODULE_TYPE_FALLBACK  = 3;
uint256 constant MODULE_TYPE_HOOK      = 4;

// ---------------------------------------------------------------------------
// validateUserOp return-code helpers.
//   0                   == VALIDATION_SUCCESS
//   1                   == invalid signature / explicit reject
//   (validAfter << 160) | (validUntil << 208) | aggregator -- packed shape
// ---------------------------------------------------------------------------
uint256 constant VALIDATION_SUCCESS = 0;
uint256 constant VALIDATION_FAILED  = 1;

// ---------------------------------------------------------------------------
// Base module surface (shared by all four module types).
// ---------------------------------------------------------------------------
interface IModule {
    function onInstall(bytes calldata data) external;
    function onUninstall(bytes calldata data) external;
    function isModuleType(uint256 moduleTypeId) external view returns (bool);
    function isInitialized(address smartAccount) external view returns (bool);
}

// ---------------------------------------------------------------------------
// Type 1 — validator. Gates ERC-4337 user-ops.
// ---------------------------------------------------------------------------
interface IValidator is IModule {
    function validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash)
        external
        returns (uint256 validationData);

    function isValidSignatureWithSender(address sender, bytes32 hash, bytes calldata signature)
        external
        view
        returns (bytes4);
}

// ---------------------------------------------------------------------------
// Type 2 — executor. Initiates calls from the account.
//   Per ERC-7579, executor modules call back into the smart account via
//   `executeFromExecutor(mode, executionCalldata)` to perform actions
//   without going through the EntryPoint user-op path. The IExecutor
//   interface itself adds no new functions beyond IModule — the
//   *account* exposes `executeFromExecutor`; modules just call into it.
//   We define our own ConstitutionExecutor that uses this pattern.
// ---------------------------------------------------------------------------
interface IExecutor is IModule {}

/// @dev Subset of the ERC-7579 account interface that executors need.
///      Real accounts (Kernel, Biconomy Nexus, Safe7579, …) implement this.
///      We don't deploy an account here, but our Executor / Hook need the
///      shape to be callable by tests.
interface IERC7579Account {
    /// @notice The account's executor-callback entry point.
    /// @param  mode             ERC-7579 packed call-type / exec-type / mode payload.
    /// @param  executionCalldata Encoded (target,value,data) for single calls or
    ///                          packed batch for batched calls. See ERC-7579 §3.
    function executeFromExecutor(bytes32 mode, bytes calldata executionCalldata)
        external
        payable
        returns (bytes[] memory returnData);
}

// ---------------------------------------------------------------------------
// Type 3 — fallback. Extends the account with new external functions.
//   When the account receives a selector it doesn't natively recognise, it
//   forwards the call to the registered fallback handler via CALL with
//   `msg.sender` of the original caller appended to calldata (ERC-2771 style).
//   The fallback decides whether to honour or revert.
// ---------------------------------------------------------------------------
interface IFallback is IModule {}

// ---------------------------------------------------------------------------
// Type 4 — hook. Runs preCheck before *every* call (regardless of which
//   validator approved it) and postCheck after. Returns opaque `hookData`
//   from preCheck that the account passes back to postCheck so the hook
//   can compare pre/post state.
//
//   This is the surface our `ConstitutionHook` (new in Phase 5 Stream M)
//   implements — the previous misnamed "ConstitutionHook" was actually
//   `IValidator` and is now `ConstitutionValidator`.
// ---------------------------------------------------------------------------
interface IHook is IModule {
    function preCheck(
        address msgSender,
        uint256 msgValue,
        bytes calldata msgData
    ) external returns (bytes memory hookData);

    function postCheck(bytes calldata hookData) external;
}
