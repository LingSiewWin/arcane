// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @title  BondVault
/// @notice Reputation-stake escrow for an agent. Follows the slash pattern
///         from Circle's arc-escrow `RefundProtocol.earlyWithdrawByArbiter`:
///         a designated oracle (arbiter) can decrement an agent's balance and
///         direct the slashed funds to an insurance address. The agent can
///         withdraw the remainder after a release window.
/// @dev    USDC on Arc is the intended asset (`0x36000…0000`), but the token
///         is a constructor arg so the tests can use a MockERC20.
contract BondVault is Ownable {
    using SafeERC20 for IERC20;

    IERC20 public immutable bondToken;

    /// @notice authorized slasher (e.g. evaluation oracle)
    address public oracle;
    /// @notice destination for slashed funds (e.g. InsuranceVault)
    address public insurance;
    /// @notice seconds an agent must wait between `post` and `release`
    uint256 public releaseWindow;

    struct Bond {
        uint256 balance;
        uint64 postedAt;
        bool oracleApprovedRelease;
    }

    /// @notice agent => bond state
    mapping(address => Bond) public bonds;

    event BondPosted(address indexed agent, uint256 amount);
    event BondSlashed(address indexed agent, uint256 amount);
    event BondReleased(address indexed agent, uint256 amount);
    event OracleUpdated(address indexed oracle);
    event InsuranceUpdated(address indexed insurance);
    event ReleaseWindowUpdated(uint256 releaseWindow);
    event ReleaseApproved(address indexed agent);

    error NotOracle();
    error ZeroAmount();
    error InsufficientBond();
    error ReleaseTooEarly(uint256 readyAt);
    error NoBond();
    error InvalidAddress();

    modifier onlyOracle() {
        if (msg.sender != oracle) revert NotOracle();
        _;
    }

    constructor(IERC20 _bondToken, address _oracle, address _insurance, uint256 _releaseWindow)
        Ownable(msg.sender)
    {
        if (address(_bondToken) == address(0)) revert InvalidAddress();
        bondToken = _bondToken;
        oracle = _oracle;
        insurance = _insurance == address(0) ? msg.sender : _insurance;
        releaseWindow = _releaseWindow;
    }

    // ------------------------------------------------------------------
    // Admin
    // ------------------------------------------------------------------

    function setOracle(address _oracle) external onlyOwner {
        oracle = _oracle;
        emit OracleUpdated(_oracle);
    }

    function setInsurance(address _insurance) external onlyOwner {
        if (_insurance == address(0)) revert InvalidAddress();
        insurance = _insurance;
        emit InsuranceUpdated(_insurance);
    }

    function setReleaseWindow(uint256 _window) external onlyOwner {
        releaseWindow = _window;
        emit ReleaseWindowUpdated(_window);
    }

    // ------------------------------------------------------------------
    // Agent (bond owner) actions
    // ------------------------------------------------------------------

    /// @notice Post `amount` of bondToken on behalf of `msg.sender`. The
    ///         caller must have approved this contract for `amount`.
    function post(uint256 amount) external {
        if (amount == 0) revert ZeroAmount();
        bondToken.safeTransferFrom(msg.sender, address(this), amount);
        Bond storage b = bonds[msg.sender];
        b.balance += amount;
        b.postedAt = uint64(block.timestamp);
        b.oracleApprovedRelease = false;
        emit BondPosted(msg.sender, amount);
    }

    /// @notice Withdraw the caller's remaining bond. Allowed after either
    ///         the release window elapses or the oracle pre-approves.
    function release() external {
        Bond storage b = bonds[msg.sender];
        uint256 bal = b.balance;
        if (bal == 0) revert NoBond();

        bool windowOpen = block.timestamp >= uint256(b.postedAt) + releaseWindow;
        if (!windowOpen && !b.oracleApprovedRelease) {
            revert ReleaseTooEarly(uint256(b.postedAt) + releaseWindow);
        }

        b.balance = 0;
        b.oracleApprovedRelease = false;
        bondToken.safeTransfer(msg.sender, bal);
        emit BondReleased(msg.sender, bal);
    }

    // ------------------------------------------------------------------
    // Oracle actions
    // ------------------------------------------------------------------

    /// @notice Slash `amount` from `agent`'s bond and forward it to the
    ///         insurance address. Mirrors the spirit of
    ///         `RefundProtocol.earlyWithdrawalByArbiter` but trimmed to the
    ///         "the arbiter has decided performance was bad, move funds out
    ///         of the agent's balance" subset.
    function slash(address agent, uint256 amount) external onlyOracle {
        if (amount == 0) revert ZeroAmount();
        Bond storage b = bonds[agent];
        if (b.balance < amount) revert InsufficientBond();

        b.balance -= amount;
        bondToken.safeTransfer(insurance, amount);
        emit BondSlashed(agent, amount);
    }

    /// @notice Pre-approve `agent` to release before the window elapses.
    ///         Useful when the oracle has finalized a positive evaluation.
    function approveRelease(address agent) external onlyOracle {
        Bond storage b = bonds[agent];
        if (b.balance == 0) revert NoBond();
        b.oracleApprovedRelease = true;
        emit ReleaseApproved(agent);
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    function balanceOf(address agent) external view returns (uint256) {
        return bonds[agent].balance;
    }

    function readyAt(address agent) external view returns (uint256) {
        return uint256(bonds[agent].postedAt) + releaseWindow;
    }
}
