// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";

/// @notice External liveness oracle contract that decides whether an agent
///         counts as "alive" (its SCA / multisig has progressed onchain).
///         Pattern is lifted from Olas' StakingActivityChecker — the checker
///         knows the agent's tx-count signal (e.g. multisig nonce), the
///         BondVault knows nothing about activity heuristics.
/// @dev    https://github.com/valory-xyz/autonolas-registries/blob/main/contracts/staking/StakingActivityChecker.sol
interface IActivityChecker {
    /// @notice Read the current activity signal for `agent` (typically a
    ///         multisig / SCA nonce). Higher = more activity.
    function getActivitySignal(address agent) external view returns (uint256);

    /// @notice Check whether the agent's activity progressed enough between
    ///         the `lastSignal` snapshot (taken `dt` seconds ago) and now.
    ///         Returns `true` if the agent is alive, `false` if dead.
    function isAlive(
        address agent,
        uint256 lastSignal,
        uint256 dt
    ) external view returns (bool);
}

/// @title  BondVault
/// @notice Reputation-stake escrow for an agent.
///
///         Phase 5 hardening (B11):
///
///         (1) PERVERSE-INCENTIVE FIX — Erasure double-burn slash.
///             Slashed bond AND a counter-bond posted by the slasher are sent
///             to the canonical burn address (0x000…dEaD). Neither the slasher
///             nor any insurance pool can profit from a slash. This is the
///             Numerai Erasure pattern documented in:
///             https://medium.com/numerai/the-erasure-protocol-awakens-48a34cc4b5d0
///             Cited in docs/benchmark_realworld.md §8.
///
///         (2) LIVENESS SIGNAL — Olas ActivityChecker probe.
///             Off-chain observers and contract callers can compute
///             `livenessExpiry(agent)` and `isAgentAlive(agent)` against an
///             external `IActivityChecker`. If the checker says the agent is
///             dead, `releaseToOperator(agent)` rescues the bond back to the
///             operator without involving the slasher. This is the
///             reward-exclusion-not-slashing posture Olas takes.
///             https://github.com/valory-xyz/autonolas-registries/blob/main/contracts/staking/StakingActivityChecker.sol
///             Cited in docs/benchmark_realworld.md §1.
///
///         (3) CIRCUIT BREAKER — OpenZeppelin Pausable.
///             Owner-gated `pause()` halts `post()` and `slash()`; `release()`
///             stays open so honest agents can rescue their funds while a
///             compromise is being investigated.
///
/// @dev    USDC on Arc is the intended asset (`0x36000…0000`), but the token
///         is a constructor arg so the tests can use a MockERC20.
contract BondVault is Ownable, Pausable {
    using SafeERC20 for IERC20;

    /// @notice Canonical burn sink for the Erasure double-burn pattern. Both
    ///         the slashed bond and the slasher's counter-bond are forwarded
    ///         here so neither party can ever profit from a slash.
    address public constant BURN_ADDRESS =
        0x000000000000000000000000000000000000dEaD;

    IERC20 public immutable bondToken;

    /// @notice authorized slasher (e.g. evaluation oracle). The slasher MUST
    ///         post a counter-bond before calling `slash` — see `slashWithCounterBond`.
    address public oracle;

    /// @notice External activity-checker contract. Optional — if zero, the
    ///         vault treats every agent as alive (no liveness signal).
    IActivityChecker public activityChecker;

    /// @notice seconds an agent must wait between `post` and `release`
    uint256 public releaseWindow;

    /// @notice seconds without activity before an agent is considered dead and
    ///         the bond may be released by anyone to the operator.
    uint256 public livenessTimeout;

    struct Bond {
        uint256 balance;
        uint64 postedAt;
        uint64 lastActivityAt;
        uint256 lastActivitySignal;
        bool oracleApprovedRelease;
        // The address that funded this bond. A dead-agent rescue
        // (`releaseToOperator`) always returns funds here — never to a
        // caller-supplied address — so the permissionless rescue can't be
        // used to redirect another agent's bond.
        address operator;
    }

    /// @notice agent => bond state
    mapping(address => Bond) public bonds;

    /// @notice slasher => counter-bond balance held in escrow against future slashes.
    ///         Burned alongside the slashed bond. Lift the counter-bond out with
    ///         `withdrawCounterBond` only if it was never spent on a slash.
    mapping(address => uint256) public counterBonds;

    event BondPosted(address indexed agent, uint256 amount);
    /// @notice Both the bond AND the counter-bond are burned. `burned` is the
    ///         sum (so off-chain observers don't have to add).
    event BondSlashed(
        address indexed agent,
        address indexed slasher,
        uint256 agentBurned,
        uint256 slasherBurned,
        uint256 burnedTotal
    );
    event BondReleased(address indexed agent, uint256 amount);
    event BondReleasedToOperator(
        address indexed agent,
        address indexed operator,
        uint256 amount,
        string reason
    );
    event OracleUpdated(address indexed oracle);
    event ActivityCheckerUpdated(address indexed checker);
    event ReleaseWindowUpdated(uint256 releaseWindow);
    event LivenessTimeoutUpdated(uint256 livenessTimeout);
    event ReleaseApproved(address indexed agent);
    event CounterBondPosted(address indexed slasher, uint256 amount, uint256 newBalance);
    event CounterBondWithdrawn(address indexed slasher, uint256 amount);
    event ActivityRefreshed(
        address indexed agent,
        uint256 signal,
        uint256 timestamp
    );

    error NotOracle();
    error ZeroAmount();
    error InsufficientBond();
    error InsufficientCounterBond();
    error ReleaseTooEarly(uint256 readyAt);
    error NoBond();
    error InvalidAddress();
    error AgentStillAlive();
    error ActivityCheckerUnset();
    error CounterBondLessThanSlash();

    modifier onlyOracle() {
        if (msg.sender != oracle) revert NotOracle();
        _;
    }

    /// @notice Deploy the BondVault.
    /// @param _bondToken        the bond asset (USDC on Arc).
    /// @param _oracle           initial slasher.
    /// @param _releaseWindow    seconds between `post()` and `release()`.
    /// @param _livenessTimeout  seconds without activity-checker progress
    ///                          before an agent is considered dead.
    constructor(
        IERC20 _bondToken,
        address _oracle,
        uint256 _releaseWindow,
        uint256 _livenessTimeout
    ) Ownable(msg.sender) {
        if (address(_bondToken) == address(0)) revert InvalidAddress();
        bondToken = _bondToken;
        oracle = _oracle;
        releaseWindow = _releaseWindow;
        livenessTimeout = _livenessTimeout;
    }

    // ------------------------------------------------------------------
    // Admin
    // ------------------------------------------------------------------

    function setOracle(address _oracle) external onlyOwner {
        oracle = _oracle;
        emit OracleUpdated(_oracle);
    }

    function setActivityChecker(address _checker) external onlyOwner {
        activityChecker = IActivityChecker(_checker);
        emit ActivityCheckerUpdated(_checker);
    }

    function setReleaseWindow(uint256 _window) external onlyOwner {
        releaseWindow = _window;
        emit ReleaseWindowUpdated(_window);
    }

    function setLivenessTimeout(uint256 _timeout) external onlyOwner {
        livenessTimeout = _timeout;
        emit LivenessTimeoutUpdated(_timeout);
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    // ------------------------------------------------------------------
    // Agent (bond owner) actions
    // ------------------------------------------------------------------

    /// @notice Post `amount` of bondToken on behalf of `msg.sender`. The
    ///         caller must have approved this contract for `amount`.
    function post(uint256 amount) external whenNotPaused {
        if (amount == 0) revert ZeroAmount();
        bondToken.safeTransferFrom(msg.sender, address(this), amount);
        Bond storage b = bonds[msg.sender];
        b.balance += amount;
        b.postedAt = uint64(block.timestamp);
        b.lastActivityAt = uint64(block.timestamp);
        b.oracleApprovedRelease = false;
        b.operator = msg.sender; // the funder; recipient of any dead-agent rescue
        // Snapshot the agent's initial activity signal so the liveness check
        // has a baseline. We tolerate a missing checker (zero) — in that case
        // liveness is purely time-based.
        if (address(activityChecker) != address(0)) {
            b.lastActivitySignal = activityChecker.getActivitySignal(msg.sender);
        }
        emit BondPosted(msg.sender, amount);
    }

    /// @notice Withdraw the caller's remaining bond. Allowed after either
    ///         the release window elapses or the oracle pre-approves.
    /// @dev    Released even when the contract is paused — pausing is a
    ///         circuit breaker against malicious oracle / admin, NOT against
    ///         agents trying to rescue their funds.
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
    // Slasher actions (Erasure double-burn)
    // ------------------------------------------------------------------

    /// @notice The slasher posts a counter-bond. Counter-bonds are pooled
    ///         per-slasher; a future `slash(agent, amount)` deducts `amount`
    ///         from the counter-bond AND burns it. This makes slashing
    ///         expensive for the slasher and impossible to profit from.
    /// @dev    Caller must have approved this contract for `amount`.
    function postCounterBond(uint256 amount) external whenNotPaused {
        if (amount == 0) revert ZeroAmount();
        bondToken.safeTransferFrom(msg.sender, address(this), amount);
        counterBonds[msg.sender] += amount;
        emit CounterBondPosted(msg.sender, amount, counterBonds[msg.sender]);
    }

    /// @notice Withdraw unused counter-bond. Slashers can pull their unused
    ///         counter-bond out at any time (counter-bond doesn't have a
    ///         release window — it's the SLASHER's collateral, not an agent's).
    function withdrawCounterBond(uint256 amount) external {
        if (amount == 0) revert ZeroAmount();
        uint256 bal = counterBonds[msg.sender];
        if (bal < amount) revert InsufficientCounterBond();
        counterBonds[msg.sender] = bal - amount;
        bondToken.safeTransfer(msg.sender, amount);
        emit CounterBondWithdrawn(msg.sender, amount);
    }

    /// @notice Slash `amount` from `agent`'s bond AND burn `amount` of the
    ///         slasher's counter-bond. Both go to BURN_ADDRESS — neither the
    ///         slasher nor any other party can profit.
    /// @dev    The slasher MUST have a counter-bond at least equal to the
    ///         slash amount. The counter-bond burn is the griefing-resistance
    ///         lever — a malicious slasher pays to slash, so frivolous slashes
    ///         drain the slasher's balance.
    function slash(address agent, uint256 amount) external onlyOracle whenNotPaused {
        if (amount == 0) revert ZeroAmount();

        Bond storage b = bonds[agent];
        if (b.balance < amount) revert InsufficientBond();

        uint256 cb = counterBonds[msg.sender];
        if (cb < amount) revert CounterBondLessThanSlash();

        // Checks-effects-interactions: clear balances before external calls.
        b.balance -= amount;
        counterBonds[msg.sender] = cb - amount;

        // Erasure double-burn: send both legs to the canonical dead address.
        // Two transfers (not one) so any subgraph or explorer indexing the
        // burn address sees a separate Transfer(agent->burn, amount) and
        // Transfer(slasher->burn, amount) line item.
        bondToken.safeTransfer(BURN_ADDRESS, amount);
        bondToken.safeTransfer(BURN_ADDRESS, amount);

        emit BondSlashed(agent, msg.sender, amount, amount, amount * 2);
    }

    /// @notice Pre-approve `agent` to release before the window elapses.
    ///         Useful when the oracle has finalized a positive evaluation.
    function approveRelease(address agent) external onlyOracle whenNotPaused {
        Bond storage b = bonds[agent];
        if (b.balance == 0) revert NoBond();
        b.oracleApprovedRelease = true;
        emit ReleaseApproved(agent);
    }

    // ------------------------------------------------------------------
    // Liveness signal (Olas ActivityChecker)
    // ------------------------------------------------------------------

    /// @notice Refresh the recorded activity snapshot for `agent`. Anyone may
    ///         call — the activity-checker contract is the trust root.
    ///         Updates `lastActivitySignal` + `lastActivityAt` if the checker
    ///         reports the agent is alive; otherwise leaves them unchanged.
    function pokeActivity(address agent) external {
        if (address(activityChecker) == address(0)) revert ActivityCheckerUnset();
        Bond storage b = bonds[agent];
        if (b.balance == 0) revert NoBond();
        uint256 current = activityChecker.getActivitySignal(agent);
        uint256 dt = block.timestamp - uint256(b.lastActivityAt);
        bool alive = activityChecker.isAlive(agent, b.lastActivitySignal, dt);
        if (alive) {
            b.lastActivitySignal = current;
            b.lastActivityAt = uint64(block.timestamp);
            emit ActivityRefreshed(agent, current, block.timestamp);
        }
    }

    /// @notice The timestamp at which `agent` is considered dead if no
    ///         activity has been observed. Returns 0 if no bond exists.
    function livenessExpiry(address agent) external view returns (uint256) {
        Bond storage b = bonds[agent];
        if (b.balance == 0) return 0;
        return uint256(b.lastActivityAt) + livenessTimeout;
    }

    /// @notice True if the agent's bond is still considered alive by the
    ///         activity-checker / liveness timeout. If no checker is set, the
    ///         time-only deadline is enforced. If no bond exists, returns false.
    function isAgentAlive(address agent) public view returns (bool) {
        Bond storage b = bonds[agent];
        if (b.balance == 0) return false;
        return block.timestamp < uint256(b.lastActivityAt) + livenessTimeout;
    }

    /// @notice Rescue a dead agent's bond back to the address that funded it.
    ///         Permissionless — anyone can call once the liveness deadline has
    ///         passed — but the recipient is the recorded `operator` (the
    ///         funder), NOT a caller-supplied address, so the rescue can't be
    ///         used to redirect/steal the bond. This matches Olas' "eviction
    ///         returns the stake" posture (we don't slash dead agents; that
    ///         would punish the operator for an infrastructure failure they
    ///         couldn't control).
    /// @dev    Allowed while paused — a paused vault must not hold operators'
    ///         bonds hostage when an agent is provably dead.
    function releaseToOperator(address agent) external {
        Bond storage b = bonds[agent];
        uint256 bal = b.balance;
        if (bal == 0) revert NoBond();
        if (isAgentAlive(agent)) revert AgentStillAlive();
        address operator = b.operator;
        if (operator == address(0)) revert InvalidAddress();

        b.balance = 0;
        bondToken.safeTransfer(operator, bal);
        emit BondReleasedToOperator(agent, operator, bal, "liveness_expired");
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

    function counterBondOf(address slasher) external view returns (uint256) {
        return counterBonds[slasher];
    }
}
