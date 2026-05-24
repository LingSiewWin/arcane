// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {IFallback, MODULE_TYPE_FALLBACK} from "./interfaces/IERC7579.sol";

/// @title  ConstitutionFallback
/// @notice ERC-7579 *fallback* module (MODULE_TYPE_FALLBACK = 3).
///
///         When a smart account receives a call to a selector it
///         doesn't natively recognise, it forwards the call to the
///         registered fallback handler. The fallback decides whether
///         to honour or revert.
///
///         The constitution's default policy is FAIL-CLOSED: any
///         selector not pre-approved by the account's owner reverts
///         with `UnknownSelector(selector)`. This prevents an attacker
///         who discovers an undocumented extension from invoking it
///         silently. The owner can pre-approve specific selectors via
///         `allow(bytes4)` (gated to the account itself) to extend
///         the account's surface deliberately.
///
/// @dev    Why this matters. Real ERC-7579 accounts (Kernel, Nexus,
///         Safe7579) ship with fallback hooks that, if installed
///         carelessly, forward unknown selectors to arbitrary
///         delegatecall targets — a single misconfiguration loses
///         the entire account. The constitution layer's posture is
///         "deny by default; allowlist explicit". An attacker who
///         lands a new selector against the account hits this
///         contract and bounces.
contract ConstitutionFallback is IFallback {
    /// @notice account => installed flag (1 = installed)
    mapping(address => bool) public installed;
    /// @notice account => set of pre-approved selectors
    mapping(address => mapping(bytes4 => bool)) public allowed;

    event FallbackInstalled(address indexed account);
    event FallbackUninstalled(address indexed account);
    event SelectorAllowed(address indexed account, bytes4 selector);
    event SelectorDisallowed(address indexed account, bytes4 selector);
    event UnknownSelectorRejected(address indexed account, bytes4 selector, address caller);

    error NotInstalled();
    error AlreadyInstalled();
    error UnknownSelector(bytes4 selector);
    error NotAccount();

    // -----------------------------------------------------------------------
    // ERC-7579 module surface
    // -----------------------------------------------------------------------

    function onInstall(bytes calldata /* data */) external override {
        if (installed[msg.sender]) revert AlreadyInstalled();
        installed[msg.sender] = true;
        emit FallbackInstalled(msg.sender);
    }

    function onUninstall(bytes calldata /* data */) external override {
        if (!installed[msg.sender]) revert NotInstalled();
        installed[msg.sender] = false;
        emit FallbackUninstalled(msg.sender);
    }

    function isModuleType(uint256 moduleTypeId) external pure override returns (bool) {
        return moduleTypeId == MODULE_TYPE_FALLBACK;
    }

    function isInitialized(address smartAccount) external view override returns (bool) {
        return installed[smartAccount];
    }

    // -----------------------------------------------------------------------
    // Allowlist management
    // -----------------------------------------------------------------------

    /// @notice Pre-approve a selector for the calling account.
    function allow(bytes4 selector) external {
        if (!installed[msg.sender]) revert NotInstalled();
        allowed[msg.sender][selector] = true;
        emit SelectorAllowed(msg.sender, selector);
    }

    /// @notice Remove a pre-approval.
    function disallow(bytes4 selector) external {
        if (!installed[msg.sender]) revert NotInstalled();
        allowed[msg.sender][selector] = false;
        emit SelectorDisallowed(msg.sender, selector);
    }

    // -----------------------------------------------------------------------
    // Fallback entry point
    // -----------------------------------------------------------------------

    /// @notice The ERC-7579 dispatch model has accounts forward
    ///         unrecognised calls to this handler. The CALL arrives
    ///         with the original calldata; we read the selector and
    ///         decide whether to honour or revert.
    ///
    ///         Honouring means: returning empty calldata (a 0-byte
    ///         "OK" response). We DO NOT delegatecall to anything —
    ///         that would defeat the policy. A real extension should
    ///         live in another module that the account installs
    ///         explicitly.
    ///
    /// @dev    msg.sender is the ACCOUNT that received the original
    ///         call. ERC-7579 spec §5 dictates accounts forward via
    ///         CALL with ERC-2771-style sender suffix; we don't need
    ///         the original caller for the deny-by-default policy.
    fallback(bytes calldata) external returns (bytes memory) {
        address account = msg.sender;
        if (!installed[account]) revert NotInstalled();

        bytes4 selector;
        // Read the first 4 bytes of the original calldata. msg.data
        // here is the forwarded calldata — selector + args.
        assembly {
            selector := calldataload(0)
        }

        if (!allowed[account][selector]) {
            emit UnknownSelectorRejected(account, selector, tx.origin);
            revert UnknownSelector(selector);
        }

        // Selector is allowlisted -- return empty bytes (no-op
        // success). Any real extension should live in another module
        // that the account installs explicitly.
        return hex"";
    }

    /// @notice Receive ETH only from accounts that have us installed.
    ///         Belt-and-braces -- the fallback above already rejects
    ///         everything else.
    receive() external payable {
        if (!installed[msg.sender]) revert NotInstalled();
    }
}
