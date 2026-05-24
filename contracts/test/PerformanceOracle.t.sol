// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {MockPyth} from "@pythnetwork/pyth-sdk-solidity/MockPyth.sol";
import {PythErrors} from "@pythnetwork/pyth-sdk-solidity/PythErrors.sol";
import {BondVault} from "../src/BondVault.sol";
import {PerformanceOracle, IBondVault} from "../src/PerformanceOracle.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

/// @notice Forge unit tests for PerformanceOracle.
///
///         MockPyth (from the pyth-sdk-solidity package) is used ONLY as a
///         deterministic test double — you cannot unit-test against live
///         market prices reproducibly. The LIVE resolution path
///         (scripts/resolve_bond.py) submits a real Hermes VAA to the real
///         Arc Pyth; that is proven separately and is never mocked at runtime.
contract PerformanceOracleTest is Test {
    MockERC20 internal usdc;
    BondVault internal vault;
    MockPyth internal pyth;
    PerformanceOracle internal oracle;

    address internal owner = address(this); // owns the vault (can setOracle)
    address internal recorder = address(0xC0DE);
    address internal alice = address(0xA11CE);
    address internal resolver = address(0xBEEF); // permissionless caller

    address internal constant BURN =
        0x000000000000000000000000000000000000dEaD;

    bytes32 internal constant SOL_USD =
        0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d;

    uint256 internal constant ONE_USDC = 1e6;
    uint256 internal constant WINDOW = 7 days;
    uint256 internal constant LIVENESS = 3 days;

    // Pyth SOL/USD scale: expo -8. $87.00 == 8_700_000_000.
    int32 internal constant EXPO = -8;
    int64 internal constant P0 = 8_700_000_000; // $87.00
    uint64 internal constant CONF_TIGHT = 8_700_000; // ~$0.087 (~10 bps)
    uint64 internal constant VALID_PERIOD = 60;
    uint256 internal constant SLASH_AMT = ONE_USDC; // 1 USDC

    function setUp() public {
        usdc = new MockERC20(6);
        // Pyth mock: validTimePeriod=60, singleUpdateFeeInWei=1 (matches Arc).
        pyth = new MockPyth(VALID_PERIOD, 1);

        // Vault with this test contract as initial oracle; we hand the oracle
        // role to PerformanceOracle below.
        vault = new BondVault(IERC20(address(usdc)), address(this), WINDOW, LIVENESS);

        oracle = new PerformanceOracle(
            // IPyth(address(pyth)) — MockPyth implements IPyth.
            pyth,
            IBondVault(address(vault)),
            IERC20(address(usdc)),
            recorder
        );
        vault.setOracle(address(oracle));

        // Fund Alice and let her post a bond.
        usdc.mint(alice, 10 * ONE_USDC);
        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);
        vm.prank(alice);
        vault.post(2 * ONE_USDC);

        // The oracle needs USDC to fund its counter-bond when it slashes.
        usdc.mint(address(oracle), 10 * ONE_USDC);

        // Resolver needs ETH to pay the (tiny) Pyth update fee.
        vm.deal(resolver, 1 ether);

        // Seed Pyth with an initial price so recordAdvice can snapshot p0.
        _setPyth(P0, CONF_TIGHT, block.timestamp);
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /// @dev Build a price-update blob and push it into MockPyth at the current
    ///      block, paying the (1 wei * n) fee.
    function _setPyth(int64 price, uint64 conf, uint256 publishTime) internal {
        bytes[] memory upd = new bytes[](1);
        upd[0] = pyth.createPriceFeedUpdateData(
            SOL_USD,
            price,
            conf,
            EXPO,
            price, // emaPrice (unused by oracle)
            conf,
            uint64(publishTime)
        );
        uint256 fee = pyth.getUpdateFee(upd);
        pyth.updatePriceFeeds{value: fee}(upd);
    }

    /// @dev Build a fresh price-update blob for resolve() WITHOUT pushing it.
    function _updateBlob(int64 price, uint64 conf, uint256 publishTime)
        internal
        view
        returns (bytes[] memory upd)
    {
        upd = new bytes[](1);
        upd[0] = pyth.createPriceFeedUpdateData(
            SOL_USD, price, conf, EXPO, price, conf, uint64(publishTime)
        );
    }

    function _record(int8 direction, uint32 thresholdBps) internal {
        // recordAdvice now refreshes Pyth first (Bug 2 fix), so pass a fresh
        // price-update blob + fee — same pattern as resolve().
        bytes[] memory upd = _updateBlob(P0, CONF_TIGHT, block.timestamp);
        uint256 fee = pyth.getUpdateFee(upd);
        vm.deal(recorder, fee);
        vm.prank(recorder);
        oracle.recordAdvice{value: fee}(
            alice, SOL_USD, direction, 1 hours, thresholdBps, SLASH_AMT, upd
        );
    }

    // ------------------------------------------------------------------
    // recordAdvice
    // ------------------------------------------------------------------

    function test_recordAdvice_snapshots_p0_from_pyth() public {
        _record(1, 200);
        (
            bytes32 feedId,
            int8 direction,
            int64 p0,
            int32 expo,
            uint64 conf0,
            uint64 recordedAt,
            uint64 horizonSecs,
            uint32 thr,
            uint256 amt,
            bool exists,
            bool resolved,
            bool slashed
        ) = oracle.advice(alice);
        assertEq(feedId, SOL_USD, "feedId");
        assertEq(direction, int8(1), "direction");
        assertEq(p0, P0, "p0 snapshot");
        assertEq(expo, EXPO, "expo");
        assertEq(conf0, CONF_TIGHT, "conf0");
        assertEq(recordedAt, uint64(block.timestamp), "recordedAt");
        assertEq(horizonSecs, uint64(1 hours), "horizon");
        assertEq(thr, uint32(200), "threshold");
        assertEq(amt, SLASH_AMT, "slashAmount");
        assertTrue(exists, "exists");
        assertFalse(resolved, "not resolved");
        assertFalse(slashed, "not slashed");
    }

    function test_recordAdvice_only_recorder() public {
        bytes[] memory upd = _updateBlob(P0, CONF_TIGHT, block.timestamp);
        vm.prank(alice);
        vm.expectRevert(PerformanceOracle.NotRecorder.selector);
        oracle.recordAdvice(alice, SOL_USD, 1, 1 hours, 200, SLASH_AMT, upd);
    }

    function test_recordAdvice_bad_direction_reverts() public {
        bytes[] memory upd = _updateBlob(P0, CONF_TIGHT, block.timestamp);
        vm.prank(recorder);
        vm.expectRevert(PerformanceOracle.InvalidDirection.selector);
        oracle.recordAdvice(alice, SOL_USD, 2, 1 hours, 200, SLASH_AMT, upd);
    }

    function test_recordAdvice_zero_horizon_reverts() public {
        bytes[] memory upd = _updateBlob(P0, CONF_TIGHT, block.timestamp);
        vm.prank(recorder);
        vm.expectRevert(PerformanceOracle.ZeroHorizon.selector);
        oracle.recordAdvice(alice, SOL_USD, 1, 0, 200, SLASH_AMT, upd);
    }

    function test_recordAdvice_fee_underpaid_reverts() public {
        // recordAdvice now pushes a Pyth update first; underpaying the fee
        // (1 wei in MockPyth) reverts FeeUnderpaid before any snapshot.
        bytes[] memory upd = _updateBlob(P0, CONF_TIGHT, block.timestamp);
        vm.prank(recorder);
        vm.expectRevert(
            abi.encodeWithSelector(PerformanceOracle.FeeUnderpaid.selector, 1, 0)
        );
        oracle.recordAdvice{value: 0}(alice, SOL_USD, 1, 1 hours, 200, SLASH_AMT, upd);
    }

    function test_recordAdvice_stale_pyth_reverts() public {
        // Push the chain well past any fresh window, then submit a price-update
        // blob whose publishTime is ALSO stale. recordAdvice pushes it, but
        // getPriceNoOlderThan still reverts StalePrice because the pushed price
        // is older than the valid period.
        skip(VALID_PERIOD + 100);
        bytes[] memory upd = _updateBlob(
            P0, CONF_TIGHT, block.timestamp - (VALID_PERIOD + 10)
        );
        uint256 fee = pyth.getUpdateFee(upd);
        vm.deal(recorder, fee);
        vm.prank(recorder);
        vm.expectRevert(PythErrors.StalePrice.selector);
        oracle.recordAdvice{value: fee}(
            alice, SOL_USD, 1, 1 hours, 200, SLASH_AMT, upd
        );
    }

    function test_recordAdvice_double_active_reverts() public {
        _record(1, 200);
        bytes[] memory upd = _updateBlob(P0, CONF_TIGHT, block.timestamp);
        uint256 fee = pyth.getUpdateFee(upd);
        vm.deal(recorder, fee);
        vm.prank(recorder);
        vm.expectRevert(PerformanceOracle.AdviceAlreadyExists.selector);
        oracle.recordAdvice{value: fee}(
            alice, SOL_USD, 1, 1 hours, 200, SLASH_AMT, upd
        );
    }

    // ------------------------------------------------------------------
    // resolve — guards
    // ------------------------------------------------------------------

    function test_resolve_before_horizon_reverts() public {
        _record(1, 200);
        bytes[] memory upd = _updateBlob(P0, CONF_TIGHT, block.timestamp);
        uint256 readyAt = oracle.resolvableAt(alice);
        vm.prank(resolver);
        vm.expectRevert(
            abi.encodeWithSelector(PerformanceOracle.HorizonNotElapsed.selector, readyAt)
        );
        oracle.resolve{value: 1}(alice, upd);
    }

    function test_resolve_no_advice_reverts() public {
        bytes[] memory upd = _updateBlob(P0, CONF_TIGHT, block.timestamp);
        vm.prank(resolver);
        vm.expectRevert(PerformanceOracle.NoAdvice.selector);
        oracle.resolve{value: 1}(address(0xDEAD), upd);
    }

    // ------------------------------------------------------------------
    // resolve — slash path
    // ------------------------------------------------------------------

    function test_resolve_slashes_when_return_below_threshold() public {
        // Long advice, threshold 200 bps. Price drops 5% -> r_bps = -500 < -200.
        _record(1, 200);
        skip(1 hours + 1);
        // p1 = $82.65 (5% below $87), tight confidence.
        int64 p1 = 8_265_000_000;
        bytes[] memory upd = _updateBlob(p1, CONF_TIGHT, block.timestamp);

        uint256 burnBefore = usdc.balanceOf(BURN);
        uint256 aliceBondBefore = vault.balanceOf(alice);

        vm.expectEmit(true, false, false, true, address(oracle));
        emit PerformanceOracle.AdviceResolved(alice, P0, p1, -500, true);

        vm.prank(resolver);
        oracle.resolve{value: 10}(alice, upd); // overpay; expect refund

        // Erasure double-burn: agent's bond AND the oracle's counter-bond burned.
        assertEq(
            vault.balanceOf(alice), aliceBondBefore - SLASH_AMT, "agent bond slashed"
        );
        assertEq(usdc.balanceOf(BURN) - burnBefore, 2 * SLASH_AMT, "both legs burned");

        (, , , , , , , , , , bool resolved, bool slashed) = oracle.advice(alice);
        assertTrue(resolved, "resolved");
        assertTrue(slashed, "slashed");
    }

    function test_resolve_refunds_overpaid_fee() public {
        _record(1, 200);
        skip(1 hours + 1);
        int64 p1 = 8_265_000_000;
        bytes[] memory upd = _updateBlob(p1, CONF_TIGHT, block.timestamp);

        uint256 balBefore = resolver.balance;
        vm.prank(resolver);
        oracle.resolve{value: 1000}(alice, upd); // fee is 1 wei, refund 999
        // Net spend should be exactly the 1 wei Pyth fee (no gas accounted in vm).
        assertEq(balBefore - resolver.balance, 1, "only the 1 wei fee consumed");
    }

    // ------------------------------------------------------------------
    // resolve — release path
    // ------------------------------------------------------------------

    function test_resolve_releases_when_within_tolerance() public {
        // Long advice, threshold 200 bps. Price rises -> r_bps positive -> pass.
        _record(1, 200);
        skip(1 hours + 1);
        int64 p1 = 9_000_000_000; // $90, up ~3.4%
        bytes[] memory upd = _updateBlob(p1, CONF_TIGHT, block.timestamp);

        uint256 burnBefore = usdc.balanceOf(BURN);

        vm.prank(resolver);
        oracle.resolve{value: 1}(alice, upd);

        assertEq(vault.balanceOf(alice), 2 * ONE_USDC, "bond untouched");
        assertEq(usdc.balanceOf(BURN), burnBefore, "nothing burned");

        (, , , , , , , , , , bool resolved, bool slashed) = oracle.advice(alice);
        assertTrue(resolved, "resolved");
        assertFalse(slashed, "passed not slashed");

        // approveRelease lets Alice pull her bond before the window.
        vm.prank(alice);
        vault.release();
        assertEq(vault.balanceOf(alice), 0, "released early");
    }

    function test_resolve_small_adverse_move_below_threshold_releases() public {
        // Long advice, threshold 200 bps. Price drops only ~1.1% -> r_bps -115
        // which is within tolerance -> NOT slashed.
        _record(1, 200);
        skip(1 hours + 1);
        int64 p1 = 8_600_000_000; // $86 vs $87 -> -114.9 bps
        bytes[] memory upd = _updateBlob(p1, CONF_TIGHT, block.timestamp);

        vm.prank(resolver);
        oracle.resolve{value: 1}(alice, upd);

        (, , , , , , , , , , , bool slashed) = oracle.advice(alice);
        assertFalse(slashed, "within tolerance, not slashed");
        assertEq(vault.balanceOf(alice), 2 * ONE_USDC, "bond untouched");
    }

    // ------------------------------------------------------------------
    // resolve — confidence-band guard
    // ------------------------------------------------------------------

    function test_resolve_confidence_band_guard_blocks_noise_slash() public {
        // Threshold 50 bps, adverse move -200 bps, but Pyth confidence is huge
        // (~400 bps). trigger = -(50 + 400) = -450; r_bps -200 > -450 -> no slash.
        _record(1, 50);
        skip(1 hours + 1);
        int64 p1 = 8_526_000_000; // $85.26 -> -200 bps vs $87
        uint64 wideConf = 348_000_000; // ~$3.48 ~= 400 bps of $87
        bytes[] memory upd = _updateBlob(p1, wideConf, block.timestamp);

        vm.prank(resolver);
        oracle.resolve{value: 1}(alice, upd);

        (, , , , , , , , , , , bool slashed) = oracle.advice(alice);
        assertFalse(slashed, "noise within conf band must NOT slash");
        assertEq(vault.balanceOf(alice), 2 * ONE_USDC, "bond untouched");
    }

    function test_resolve_slashes_when_move_exceeds_conf_band() public {
        // Same threshold 50 bps but the adverse move (-500 bps) clears even a
        // wide conf band (~400 bps): trigger = -(50+400)=-450; -500 < -450 slash.
        _record(1, 50);
        skip(1 hours + 1);
        int64 p1 = 8_265_000_000; // -500 bps
        uint64 wideConf = 348_000_000; // ~400 bps
        bytes[] memory upd = _updateBlob(p1, wideConf, block.timestamp);

        vm.prank(resolver);
        oracle.resolve{value: 1}(alice, upd);

        (, , , , , , , , , , , bool slashed) = oracle.advice(alice);
        assertTrue(slashed, "move beyond conf band slashes");
        assertEq(vault.balanceOf(alice), 2 * ONE_USDC - SLASH_AMT, "slashed");
    }

    // ------------------------------------------------------------------
    // short direction
    // ------------------------------------------------------------------

    function test_resolve_short_slashes_on_price_rise() public {
        // Short advice (-1): a price RISE is adverse. +6% rise -> r_bps -600.
        _record(-1, 200);
        skip(1 hours + 1);
        int64 p1 = 9_222_000_000; // ~+6% from $87
        bytes[] memory upd = _updateBlob(p1, CONF_TIGHT, block.timestamp);

        vm.prank(resolver);
        oracle.resolve{value: 1}(alice, upd);

        (, , , , , , , , , , , bool slashed) = oracle.advice(alice);
        assertTrue(slashed, "short slashed on adverse rise");
    }

    // ------------------------------------------------------------------
    // Erasure invariant: oracle without counter-bond funds can't slash
    // ------------------------------------------------------------------

    function test_resolve_oracle_without_counter_bond_funds_reverts() public {
        // Drain the oracle's USDC so it cannot fund a counter-bond. The
        // Erasure invariant means: no skin in the game -> no slash.
        uint256 oracleBal = usdc.balanceOf(address(oracle));
        vm.prank(address(oracle));
        usdc.transfer(address(0x1), oracleBal);

        _record(1, 200);
        skip(1 hours + 1);
        int64 p1 = 8_265_000_000; // -500 bps, would otherwise slash
        bytes[] memory upd = _updateBlob(p1, CONF_TIGHT, block.timestamp);

        vm.prank(resolver);
        vm.expectRevert(PerformanceOracle.InsufficientCounterBond.selector);
        oracle.resolve{value: 1}(alice, upd);

        // Advice must NOT be marked resolved on a reverted resolve.
        (, , , , , , , , , , bool resolved, ) = oracle.advice(alice);
        assertFalse(resolved, "reverted resolve leaves advice unresolved");
    }

    // ------------------------------------------------------------------
    // Idempotency: double-resolve reverts
    // ------------------------------------------------------------------

    function test_double_resolve_reverts() public {
        _record(1, 200);
        skip(1 hours + 1);
        int64 p1 = 9_000_000_000; // pass path
        bytes[] memory upd = _updateBlob(p1, CONF_TIGHT, block.timestamp);

        vm.prank(resolver);
        oracle.resolve{value: 1}(alice, upd);

        bytes[] memory upd2 = _updateBlob(p1, CONF_TIGHT, block.timestamp + 1);
        vm.prank(resolver);
        vm.expectRevert(PerformanceOracle.AlreadyResolved.selector);
        oracle.resolve{value: 1}(alice, upd2);
    }

    // ------------------------------------------------------------------
    // fee underpayment
    // ------------------------------------------------------------------

    function test_resolve_fee_underpaid_reverts() public {
        _record(1, 200);
        skip(1 hours + 1);
        bytes[] memory upd = _updateBlob(8_265_000_000, CONF_TIGHT, block.timestamp);
        vm.prank(resolver);
        vm.expectRevert(
            abi.encodeWithSelector(PerformanceOracle.FeeUnderpaid.selector, 1, 0)
        );
        oracle.resolve{value: 0}(alice, upd);
    }

    // ------------------------------------------------------------------
    // reentrancy guard
    // ------------------------------------------------------------------

    /// @notice A malicious resolver that reenters resolve() for a SECOND agent
    ///         on the ETH-refund callback must be blocked by nonReentrant. The
    ///         nested call reverts (caught here), and the second agent stays
    ///         unresolved — no nested state mutation slips through.
    function test_resolve_reentrancy_blocked() public {
        address bob = address(0xB0B2);
        // Record advice for both alice and bob (long, generous threshold → the
        // favorable resolution below takes the no-slash branch).
        _record(1, 200);
        bytes[] memory brec = _updateBlob(P0, CONF_TIGHT, block.timestamp);
        uint256 brecFee = pyth.getUpdateFee(brec);
        vm.deal(recorder, brecFee);
        vm.prank(recorder);
        oracle.recordAdvice{value: brecFee}(bob, SOL_USD, 1, 1 hours, 200, SLASH_AMT, brec);

        skip(1 hours + 1);

        // Favorable price (+1.1%) → both resolutions would pass (no slash).
        int64 p1 = 8_800_000_000;
        bytes[] memory aliceBlob = _updateBlob(p1, CONF_TIGHT, block.timestamp);
        bytes[] memory bobBlob = _updateBlob(p1, CONF_TIGHT, block.timestamp);

        ReentrantResolver attacker = new ReentrantResolver(oracle);
        vm.deal(address(attacker), 1 ether);
        attacker.arm(bob, bobBlob, 1); // reenter resolve(bob) with a 1-wei fee

        // Outer resolve forwards all balance → oracle refunds the excess →
        // triggers the attacker's receive(), which attempts the nested resolve.
        attacker.attack(alice, aliceBlob);

        assertTrue(attacker.reenterReverted(), "nested resolve should be guarded");
        (, , , , , , , , , , bool aliceResolved, ) = oracle.advice(alice);
        (, , , , , , , , , , bool bobResolved, ) = oracle.advice(bob);
        assertTrue(aliceResolved, "outer resolve completed");
        assertFalse(bobResolved, "nested resolve must NOT have taken effect");
    }
}

/// @dev Attacker used by test_resolve_reentrancy_blocked. On the ETH refund it
///      tries to reenter resolve() for a different agent; the guard must revert
///      that nested call (caught so the outer call still completes).
contract ReentrantResolver {
    PerformanceOracle public immutable oracle;
    address public reenterAgent;
    bytes[] internal reenterBlob;
    uint256 public reenterFee;
    bool public tried;
    bool public reenterReverted;

    constructor(PerformanceOracle _oracle) {
        oracle = _oracle;
    }

    function arm(address agent, bytes[] calldata blob, uint256 fee) external {
        reenterAgent = agent;
        delete reenterBlob;
        for (uint256 i; i < blob.length; i++) reenterBlob.push(blob[i]);
        reenterFee = fee;
    }

    function attack(address agent, bytes[] calldata blob) external {
        oracle.resolve{value: address(this).balance}(agent, blob);
    }

    receive() external payable {
        if (!tried) {
            tried = true;
            try oracle.resolve{value: reenterFee}(reenterAgent, reenterBlob) {
                reenterReverted = false;
            } catch {
                reenterReverted = true;
            }
        }
    }
}
