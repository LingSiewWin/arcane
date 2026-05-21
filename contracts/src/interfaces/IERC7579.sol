// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

/// @notice Minimal ERC-7579 types/interfaces used by ConstitutionHook.
/// @dev    We intentionally do not depend on `@rhinestone/modulekit`; the
///         hackathon scope only needs the IValidator surface plus the
///         PackedUserOperation struct shape used by ERC-4337 v0.7.

/// @dev ERC-4337 v0.7 packed user-op layout. Mirrors EntryPoint's struct.
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

/// @dev ERC-7579 module type ID for validators.
uint256 constant MODULE_TYPE_VALIDATOR = 1;

/// @notice Subset of the ERC-7579 module interface we implement.
interface IModule {
    function onInstall(bytes calldata data) external;
    function onUninstall(bytes calldata data) external;
    function isModuleType(uint256 moduleTypeId) external view returns (bool);
    function isInitialized(address smartAccount) external view returns (bool);
}

/// @notice Subset of the ERC-7579 validator interface.
interface IValidator is IModule {
    /// @dev Returns ERC-4337 validation data. 0 == valid; 1 == invalid sig;
    ///      otherwise (validAfter << 160 | validUntil << 208 | aggregator).
    function validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash)
        external
        returns (uint256 validationData);

    /// @dev ERC-1271 style signature validation (not used in this build).
    function isValidSignatureWithSender(address sender, bytes32 hash, bytes calldata signature)
        external
        view
        returns (bytes4);
}

// Constants for validateUserOp's return code.
uint256 constant VALIDATION_SUCCESS = 0;
uint256 constant VALIDATION_FAILED = 1;
