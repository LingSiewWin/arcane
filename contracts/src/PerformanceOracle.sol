// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IPyth} from "@pythnetwork/pyth-sdk-solidity/IPyth.sol";
import {PythStructs} from "@pythnetwork/pyth-sdk-solidity/PythStructs.sol";

/// @notice Minimal view of the BondVault surface this oracle drives. The
///         oracle IS the vault's `oracle` (set via `vault.setOracle`), and
///         it MUST post its own counter-bond before it can slash — the
///         Erasure double-burn invariant. See BondVault.sol.
interface IBondVault {
    function slash(address agent, uint256 amount) external;

    function postCounterBond(uint256 amount) external;

    function counterBondOf(address slasher) external view returns (uint256);

    function balanceOf(address agent) external view returns (uint256);

    function approveRelease(address agent) external;
}

/// @title  PerformanceOracle
/// @notice A Pyth-price-driven, permissionless performance verdict for bonded
///         trading advice. Replaces the hardcoded `slash(deployer, 100000)`
///         stub with a deterministic, market-driven slash rule:
///
///           r_bps = direction * (p1 - p0) * 10000 / |p0|
///
///         where `p0` is the Pyth price snapshotted at advice time and `p1`
///         is a FRESH Pyth price pulled at resolution. If the realized return
///         is adverse by more than `slashThresholdBps` (and the adverse move
///         exceeds the Pyth confidence band so noise can't trigger a slash),
///         the oracle posts its own counter-bond and slashes the agent via
///         the Erasure double-burn vault. Otherwise the advice passed and the
///         agent's bond is releasable.
///
/// @dev    Resolution is PERMISSIONLESS after the horizon — anyone can submit
///         a fresh Hermes VAA and pull the verdict on-chain. The oracle pays
///         the Pyth update fee from `msg.value` and refunds the excess. The
///         oracle has skin in the game: every slash burns an equal counter-bond
///         it posted, so a frivolous oracle drains its own balance.
contract PerformanceOracle {
    using SafeERC20 for IERC20;

    IPyth public immutable pyth;
    IBondVault public immutable vault;
    /// @notice The bond asset (USDC on Arc). Must match `vault.bondToken`.
    IERC20 public immutable bondToken;

    /// @notice Account allowed to record advice. The agent doesn't self-report;
    ///         an operator/orchestrator commits the advice on the agent's behalf.
    address public immutable recorder;

    struct Advice {
        bytes32 feedId;
        int8 direction; // +1 long, -1 short
        int64 p0; // Pyth price at advice time
        int32 expo; // Pyth exponent (same scale used for p1)
        uint64 conf0; // Pyth confidence at advice time
        uint64 recordedAt;
        uint64 horizonSecs;
        uint32 slashThresholdBps; // tolerance: slash iff r_bps < -threshold
        uint256 slashAmount; // bond units to slash on failure
        bool exists;
        bool resolved;
        bool slashed;
    }

    /// @notice agent => their single outstanding advice commitment.
    mapping(address => Advice) public advice;

    event AdviceRecorded(
        address indexed agent,
        bytes32 indexed feedId,
        int8 direction,
        int64 p0,
        int32 expo,
        uint64 conf0,
        uint64 recordedAt,
        uint64 horizonSecs,
        uint32 slashThresholdBps,
        uint256 slashAmount
    );

    event AdviceResolved(
        address indexed agent,
        int64 p0,
        int64 p1,
        int256 rBps,
        bool slashed
    );

    error NotRecorder();
    error InvalidDirection();
    error ZeroHorizon();
    error ZeroSlashAmount();
    error AdviceAlreadyExists();
    error NoAdvice();
    error AlreadyResolved();
    error HorizonNotElapsed(uint256 readyAt);
    error PriceFeedMismatch();
    error FeeUnderpaid(uint256 required, uint256 provided);
    error InsufficientCounterBond();
    error RefundFailed();

    modifier onlyRecorder() {
        if (msg.sender != recorder) revert NotRecorder();
        _;
    }

    /// @param _pyth      Pyth pull-oracle contract (Arc: 0x2880aB155794e7179c9eE2e38200202908C17B43).
    /// @param _vault     BondVault this oracle slashes against. The oracle must
    ///                   be set as `vault.oracle` (owner calls `vault.setOracle`).
    /// @param _bondToken bond asset; must equal `vault.bondToken`.
    /// @param _recorder  account authorized to call `recordAdvice`.
    constructor(IPyth _pyth, IBondVault _vault, IERC20 _bondToken, address _recorder) {
        require(address(_pyth) != address(0), "pyth=0");
        require(address(_vault) != address(0), "vault=0");
        require(address(_bondToken) != address(0), "token=0");
        require(_recorder != address(0), "recorder=0");
        pyth = _pyth;
        vault = _vault;
        bondToken = _bondToken;
        recorder = _recorder;
    }

    // ------------------------------------------------------------------
    // Record
    // ------------------------------------------------------------------

    /// @notice Commit `agent`'s trading advice. Pushes a FRESH Pyth price from
    ///         `priceUpdate` (a Hermes VAA) on-chain, then snapshots `p0` from
    ///         that price. One outstanding advice per agent at a time.
    ///
    ///         The caller pays the Pyth update fee from `msg.value`; any excess
    ///         is refunded. This mirrors `resolve()` — recording advice MUST
    ///         refresh the price first, because on a live chain the most recent
    ///         on-chain Pyth price is frequently older than the valid time
    ///         period, which would make `getPriceNoOlderThan` revert
    ///         `StalePrice`.
    ///
    /// @param agent              the bonded agent whose advice this is.
    /// @param feedId             Pyth feed id (e.g. SOL/USD).
    /// @param direction          +1 long, -1 short.
    /// @param horizonSecs        seconds until the advice can be resolved.
    /// @param slashThresholdBps  adverse-move tolerance in bps before a slash.
    /// @param slashAmount        bond units burned on failure (== oracle's
    ///                           counter-bond burned alongside it).
    /// @param priceUpdate        the Hermes VAA(s) for `updatePriceFeeds`.
    function recordAdvice(
        address agent,
        bytes32 feedId,
        int8 direction,
        uint64 horizonSecs,
        uint32 slashThresholdBps,
        uint256 slashAmount,
        bytes[] calldata priceUpdate
    ) external payable onlyRecorder {
        if (direction != 1 && direction != -1) revert InvalidDirection();
        if (horizonSecs == 0) revert ZeroHorizon();
        if (slashAmount == 0) revert ZeroSlashAmount();
        if (advice[agent].exists && !advice[agent].resolved) {
            revert AdviceAlreadyExists();
        }

        // Pay the Pyth update fee and push the fresh price on-chain BEFORE
        // snapshotting p0 — exactly as resolve() does. Without this, on a live
        // chain getPriceNoOlderThan reverts StalePrice when the latest on-chain
        // price is older than the valid time period.
        uint256 fee = pyth.getUpdateFee(priceUpdate);
        if (msg.value < fee) revert FeeUnderpaid(fee, msg.value);
        pyth.updatePriceFeeds{value: fee}(priceUpdate);

        // Snapshot p0 from the now-fresh Pyth price. Reverts (StalePrice /
        // PriceFeedNotFound) only if the pushed price is itself stale.
        PythStructs.Price memory p = pyth.getPriceNoOlderThan(
            feedId,
            pyth.getValidTimePeriod()
        );

        advice[agent] = Advice({
            feedId: feedId,
            direction: direction,
            p0: p.price,
            expo: p.expo,
            conf0: p.conf,
            recordedAt: uint64(block.timestamp),
            horizonSecs: horizonSecs,
            slashThresholdBps: slashThresholdBps,
            slashAmount: slashAmount,
            exists: true,
            resolved: false,
            slashed: false
        });

        emit AdviceRecorded(
            agent,
            feedId,
            direction,
            p.price,
            p.expo,
            p.conf,
            uint64(block.timestamp),
            horizonSecs,
            slashThresholdBps,
            slashAmount
        );

        // Refund any overpayment of the Pyth update fee (mirrors resolve()).
        uint256 refund = msg.value - fee;
        if (refund > 0) {
            (bool ok, ) = msg.sender.call{value: refund}("");
            if (!ok) revert RefundFailed();
        }
    }

    // ------------------------------------------------------------------
    // Resolve (permissionless after horizon)
    // ------------------------------------------------------------------

    /// @notice Resolve `agent`'s advice. Permissionless once the horizon has
    ///         elapsed. Pulls a FRESH Pyth price from `priceUpdate` (a Hermes
    ///         VAA), computes the realized return, and either slashes the
    ///         agent (Erasure double-burn) or marks the advice passed.
    ///
    ///         The caller pays the Pyth update fee from `msg.value`; any
    ///         excess is refunded. Idempotent — a second resolve reverts.
    ///
    /// @param agent       the agent whose advice to resolve.
    /// @param priceUpdate the Hermes VAA(s) for `updatePriceFeeds`.
    function resolve(address agent, bytes[] calldata priceUpdate) external payable {
        Advice storage a = advice[agent];
        if (!a.exists) revert NoAdvice();
        if (a.resolved) revert AlreadyResolved();

        uint256 readyAt = uint256(a.recordedAt) + uint256(a.horizonSecs);
        if (block.timestamp < readyAt) revert HorizonNotElapsed(readyAt);

        // Pay the Pyth update fee and push the fresh price on-chain.
        uint256 fee = pyth.getUpdateFee(priceUpdate);
        if (msg.value < fee) revert FeeUnderpaid(fee, msg.value);
        pyth.updatePriceFeeds{value: fee}(priceUpdate);

        // Mark resolved BEFORE external slash interaction (CEI / reentrancy).
        a.resolved = true;

        // Pull the fresh, sufficiently-recent price.
        PythStructs.Price memory p1 = pyth.getPriceNoOlderThan(
            a.feedId,
            pyth.getValidTimePeriod()
        );
        if (p1.expo != a.expo) {
            // Exponent must match so p0 and p1 share a scale. A feed expo can
            // in principle drift between snapshots; if it does, the bps math
            // would be silently wrong. Refuse rather than mis-slash.
            revert PriceFeedMismatch();
        }

        int256 rBps = _returnBps(a.p0, p1.price, a.direction);

        bool slashed = _shouldSlash(rBps, a.slashThresholdBps, p1.conf, a.p0);
        if (slashed) {
            // Erasure double-burn: the oracle must post its own counter-bond
            // before it can slash. We bond exactly the slash amount here so the
            // oracle always has skin in the game equal to the slash.
            if (vault.counterBondOf(address(this)) < a.slashAmount) {
                uint256 need = a.slashAmount - vault.counterBondOf(address(this));
                if (bondToken.balanceOf(address(this)) < need) {
                    revert InsufficientCounterBond();
                }
                bondToken.forceApprove(address(vault), need);
                vault.postCounterBond(need);
            }
            vault.slash(agent, a.slashAmount);
            a.slashed = true;
        } else {
            // Advice passed — let the agent pull the bond before the window.
            vault.approveRelease(agent);
        }

        // Refund any overpayment of the Pyth update fee.
        uint256 refund = msg.value - fee;
        if (refund > 0) {
            (bool ok, ) = msg.sender.call{value: refund}("");
            if (!ok) revert RefundFailed();
        }

        emit AdviceResolved(agent, a.p0, p1.price, rBps, slashed);
    }

    // ------------------------------------------------------------------
    // Verdict math
    // ------------------------------------------------------------------

    /// @notice Signed realized return in basis points:
    ///         r_bps = direction * (p1 - p0) * 10000 / |p0|.
    ///         p0 and p1 share the same Pyth exponent, so the ratio is
    ///         exponent-invariant and no rescaling is needed.
    function _returnBps(int64 p0, int64 p1, int8 direction)
        internal
        pure
        returns (int256)
    {
        int256 denom = p0 >= 0 ? int256(p0) : -int256(p0);
        require(denom != 0, "p0=0");
        int256 delta = int256(p1) - int256(p0);
        return (int256(direction) * delta * 10000) / denom;
    }

    /// @notice Slash iff the advice was adverse by more than the threshold AND
    ///         the adverse move clears the Pyth confidence band (so price noise
    ///         within the reported uncertainty never triggers a slash).
    /// @dev    The confidence band is converted to bps against |p0| and added
    ///         to the threshold: the realized loss must exceed
    ///         (threshold + confBps) to slash.
    function _shouldSlash(
        int256 rBps,
        uint32 slashThresholdBps,
        uint64 conf1,
        int64 p0
    ) internal pure returns (bool) {
        if (rBps >= 0) return false; // advice was right or flat
        int256 denom = p0 >= 0 ? int256(p0) : -int256(p0);
        require(denom != 0, "p0=0");
        // Confidence as bps of |p0|. Noise within this band is not a real loss.
        int256 confBps = (int256(uint256(conf1)) * 10000) / denom;
        int256 trigger = -(int256(uint256(slashThresholdBps)) + confBps);
        return rBps < trigger;
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    /// @notice True once the advice horizon has elapsed and it can be resolved.
    function isResolvable(address agent) external view returns (bool) {
        Advice storage a = advice[agent];
        if (!a.exists || a.resolved) return false;
        return block.timestamp >= uint256(a.recordedAt) + uint256(a.horizonSecs);
    }

    /// @notice Timestamp at which `agent`'s advice becomes resolvable.
    function resolvableAt(address agent) external view returns (uint256) {
        Advice storage a = advice[agent];
        if (!a.exists) return 0;
        return uint256(a.recordedAt) + uint256(a.horizonSecs);
    }
}
