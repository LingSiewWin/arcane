// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Colosseum} from "../src/Colosseum.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract ColosseumTest is Test {
    MockERC20 internal usdc;
    Colosseum internal arena;

    address internal recorder = address(0xBEEF);
    address internal treasury = address(0x7EA5);
    address internal agentA = address(0xA1);
    address internal agentB = address(0xB2);
    address internal devA = address(0xDA); // developer who staked agentA
    address internal devB = address(0xDB); // developer who staked agentB
    address internal alice = address(0xA11CE);
    address internal bob = address(0xB0B);
    address internal carol = address(0xCA401);

    uint256 internal constant USDC = 1e6;
    uint256 internal STAKE; // arena.stakeRequirement(), cached
    // Cached item kinds — read once here so they don't consume a vm.prank when
    // used as a call argument inside a pranked statement.
    uint8 internal FLASH;
    uint8 internal WIPE;
    uint8 internal SHIELD;

    function setUp() public {
        usdc = new MockERC20(6);
        arena = new Colosseum(IERC20(address(usdc)), recorder, treasury);
        FLASH = arena.ITEM_FLASHBANG();
        WIPE = arena.ITEM_MEMORY_WIPE();
        SHIELD = arena.ITEM_LIQUIDITY_SHIELD();
        STAKE = arena.stakeRequirement();
        _fund(alice);
        _fund(bob);
        _fund(carol);
        _fund(devA);
        _fund(devB);
        // Both agents are staked into the arena by their developers.
        vm.prank(devA);
        arena.registerAgent(agentA);
        vm.prank(devB);
        arena.registerAgent(agentB);
    }

    function _fund(address u) internal {
        usdc.mint(u, 1000 * USDC);
        vm.prank(u);
        usdc.approve(address(arena), type(uint256).max);
    }

    // Trading starts immediately (no betting window) — for chaos/score tests.
    function _liveDuel() internal returns (uint256 id) {
        vm.prank(recorder);
        id = arena.createDuel(agentA, agentB, 0, 1 hours);
    }

    // 1h betting window then 1h trading — for bet/claim tests.
    function _bettingDuel() internal returns (uint256 id) {
        vm.prank(recorder);
        id = arena.createDuel(agentA, agentB, 1 hours, 1 hours);
    }

    // ---- registration -------------------------------------------------

    function test_registerAgent_pullsStakeAndRecords() public {
        address agentC = address(0xC3);
        uint256 before = usdc.balanceOf(devA);
        vm.prank(devA);
        arena.registerAgent(agentC);
        (address developer, uint256 stake, uint256 failures, bool registered) = arena.agents(agentC);
        assertEq(developer, devA);
        assertEq(stake, STAKE);
        assertEq(failures, 0);
        assertTrue(registered);
        assertEq(before - usdc.balanceOf(devA), STAKE);
    }

    function test_registerAgent_rejectsDouble() public {
        vm.prank(devA);
        vm.expectRevert(Colosseum.AlreadyRegistered.selector);
        arena.registerAgent(agentA); // already registered in setUp
    }

    function test_createDuel_requiresRegistered() public {
        address unstaked = address(0xDEAD01);
        vm.prank(recorder);
        vm.expectRevert(Colosseum.NotRegistered.selector);
        arena.createDuel(agentA, unstaked, 0, 1 hours);
    }

    // ---- create -------------------------------------------------------

    function test_createDuel_setsFields() public {
        uint256 id = _bettingDuel();
        Colosseum.Duel memory d = arena.getDuel(id);
        assertEq(d.agentA, agentA);
        assertEq(d.agentB, agentB);
        assertEq(uint8(d.status), uint8(Colosseum.Status.Live));
        assertEq(d.tradingStartsAt, d.startAt + 1 hours);
        assertEq(d.endsAt, d.tradingStartsAt + 1 hours);
        assertEq(arena.duelCount(), 1);
    }

    function test_createDuel_onlyRecorder() public {
        vm.expectRevert(Colosseum.NotRecorder.selector);
        arena.createDuel(agentA, agentB, 0, 1 hours);
    }

    function test_createDuel_rejects_sameAndZero() public {
        vm.startPrank(recorder);
        vm.expectRevert(Colosseum.SameAgent.selector);
        arena.createDuel(agentA, agentA, 0, 1 hours);
        vm.expectRevert(Colosseum.ZeroAgent.selector);
        arena.createDuel(address(0), agentB, 0, 1 hours);
        vm.expectRevert(Colosseum.ZeroAmount.selector); // tradingSecs == 0
        arena.createDuel(agentA, agentB, 60, 0);
        vm.stopPrank();
    }

    function test_phase_gating() public {
        uint256 id = _bettingDuel(); // betting open 1h, then trading 1h
        // Chaos can't land during the betting window.
        vm.prank(alice);
        vm.expectRevert(Colosseum.TradingNotStarted.selector);
        arena.injectChaos(id, agentA, FLASH);
        // Bet is fine now.
        vm.prank(alice);
        arena.bet(id, true, USDC);
        // Warp into the trading phase: betting closes, chaos opens.
        vm.warp(block.timestamp + 1 hours + 1);
        vm.prank(bob);
        vm.expectRevert(Colosseum.BettingClosed.selector);
        arena.bet(id, true, USDC);
        vm.prank(alice);
        arena.injectChaos(id, agentA, FLASH); // now allowed
    }

    // ---- betting (parimutuel) ----------------------------------------

    function test_bet_updatesPools() public {
        uint256 id = _bettingDuel();
        vm.prank(alice);
        arena.bet(id, true, 2 * USDC);
        vm.prank(bob);
        arena.bet(id, false, 3 * USDC);
        Colosseum.Duel memory d = arena.getDuel(id);
        assertEq(d.poolA, 2 * USDC);
        assertEq(d.poolB, 3 * USDC);
        assertEq(arena.betOf(id, true, alice), 2 * USDC);
        // Contract holds the 5 USDC of bets + the two registration stakes.
        assertEq(usdc.balanceOf(address(arena)), 5 * USDC + 2 * STAKE);
    }

    function test_bet_onlyLive() public {
        vm.expectRevert(Colosseum.DuelDoesNotExist.selector);
        vm.prank(alice);
        arena.bet(99, true, USDC);
    }

    // ---- chaos injections (escrow + operator cut) --------------------

    function test_injectChaos_splitsFeeAndEscrows() public {
        uint256 id = _liveDuel();
        uint256 treasuryBefore = usdc.balanceOf(treasury);
        vm.prank(alice);
        uint256 inj1 = arena.injectChaos(id, agentA, FLASH); // 0.5 USDC
        vm.prank(bob);
        uint256 inj2 = arena.injectChaos(id, agentA, WIPE); // 1.0 USDC
        assertEq(inj1, 1);
        assertEq(inj2, 2);
        // 10% operator cut to treasury: 0.05 + 0.10 = 0.15 USDC.
        assertEq(usdc.balanceOf(treasury) - treasuryBefore, 150_000);
        // Remainder escrowed per injection.
        assertEq(arena.injectionEscrows(inj1), 450_000);
        assertEq(arena.injectionEscrows(inj2), 900_000);
        assertEq(arena.injectionTarget(inj1), agentA);
        assertEq(arena.injectionsAgainst(id, agentA), 2);
    }

    function test_injectChaos_rejectsBadInputs() public {
        uint256 id = _liveDuel();
        vm.startPrank(alice);
        vm.expectRevert(Colosseum.UnknownAgent.selector);
        arena.injectChaos(id, address(0xdead), 0);
        vm.expectRevert(Colosseum.BadItem.selector);
        arena.injectChaos(id, agentA, 9);
        vm.stopPrank();
        // Disable an item → ItemDisabled.
        vm.prank(recorder);
        arena.setItemPrice(FLASH, 0);
        vm.prank(alice);
        vm.expectRevert(Colosseum.ItemDisabled.selector);
        arena.injectChaos(id, agentA, FLASH);
    }

    // ---- scoring + resilience ----------------------------------------

    function test_reportCall_accumulatesScoreAndResilience() public {
        uint256 id = _liveDuel();
        vm.startPrank(recorder);
        arena.reportCall(id, agentA, 0, 50, true, true, false); // survived an injection
        arena.reportCall(id, agentA, 0, -30, true, false, false); // ate one, blew up
        arena.reportCall(id, agentB, 0, 10, false, false, false); // clean call
        vm.stopPrank();
        Colosseum.Duel memory d = arena.getDuel(id);
        assertEq(d.scoreA, 20);
        assertEq(d.scoreB, 10);
        (uint256 ing, uint256 surv) = arena.resilienceOf(agentA);
        assertEq(ing, 2);
        assertEq(surv, 1);
    }

    function test_reportCall_failedBumpsFailures() public {
        uint256 id = _liveDuel();
        vm.startPrank(recorder);
        arena.reportCall(id, agentB, 0, -100, false, false, true); // model failure
        arena.reportCall(id, agentB, 0, -100, false, false, true);
        vm.stopPrank();
        (, , uint256 failures, ) = arena.agents(agentB);
        assertEq(failures, 2);
    }

    function test_reportReasoning_emitsForKnownAgent() public {
        uint256 id = _liveDuel();
        vm.expectEmit(true, true, false, true, address(arena));
        emit Colosseum.AgentReasoning(id, agentA, 1, true, true, "resisted the flashbang");
        vm.prank(recorder);
        arena.reportReasoning(id, agentA, 1, true, true, "resisted the flashbang");

        vm.expectRevert(Colosseum.NotRecorder.selector);
        arena.reportReasoning(id, agentA, 1, false, false, "x");
        vm.prank(recorder);
        vm.expectRevert(Colosseum.UnknownAgent.selector);
        arena.reportReasoning(id, address(0xdead), 1, false, false, "x");
    }

    function test_reportCall_onlyRecorder_andKnownAgent() public {
        uint256 id = _liveDuel();
        vm.expectRevert(Colosseum.NotRecorder.selector);
        arena.reportCall(id, agentA, 0, 1, false, false, false);
        vm.prank(recorder);
        vm.expectRevert(Colosseum.UnknownAgent.selector);
        arena.reportCall(id, address(0xdead), 0, 1, false, false, false);
    }

    // ---- defense bounty (escrow routing) ------------------------------

    function test_bounty_paidToDeveloperOnSurvive() public {
        uint256 id = _liveDuel();
        vm.prank(alice);
        uint256 inj = arena.injectChaos(id, agentB, FLASH); // escrow 0.45 USDC
        uint256 devBefore = usdc.balanceOf(devB);
        vm.prank(recorder);
        arena.reportCall(id, agentB, inj, 10, true, true, false); // survived → bounty
        assertEq(usdc.balanceOf(devB) - devBefore, 450_000);
        assertEq(arena.injectionEscrows(inj), 0); // settled
    }

    function test_bounty_toPoolOnFooled() public {
        uint256 id = _liveDuel();
        vm.prank(alice);
        uint256 inj = arena.injectChaos(id, agentB, FLASH); // escrow 0.45 USDC
        uint256 devBefore = usdc.balanceOf(devB);
        vm.prank(recorder);
        arena.reportCall(id, agentB, inj, -20, true, false, false); // fooled → pool
        assertEq(usdc.balanceOf(devB), devBefore); // dev got nothing
        assertEq(arena.prizePool(id), 450_000);
        assertEq(arena.injectionEscrows(inj), 0);
    }

    function test_reportCall_injectionTargetMismatchReverts() public {
        uint256 id = _liveDuel();
        vm.prank(alice);
        uint256 inj = arena.injectChaos(id, agentB, FLASH);
        vm.prank(recorder);
        vm.expectRevert(Colosseum.InjectionAgentMismatch.selector);
        arena.reportCall(id, agentA, inj, 10, true, true, false); // inj targeted B
    }

    // ---- resolve (dual prize + stake settlement) ----------------------

    function test_resolve_setsAlphaAndShieldWinners() public {
        uint256 id = _liveDuel();
        // A: faces 1 injection, fooled (resilience 0/1). B: faces 1, survives (1/1).
        vm.prank(alice);
        uint256 injA = arena.injectChaos(id, agentA, FLASH);
        vm.prank(bob);
        uint256 injB = arena.injectChaos(id, agentB, FLASH);
        vm.startPrank(recorder);
        arena.reportCall(id, agentA, injA, 100, true, false, false); // A high PnL, fooled
        arena.reportCall(id, agentB, injB, 10, true, true, false);   // B low PnL, resilient
        vm.stopPrank();

        vm.expectRevert(Colosseum.DuelNotOver.selector);
        arena.resolve(id);

        vm.warp(block.timestamp + 1 hours + 1);
        arena.resolve(id);
        Colosseum.Duel memory d = arena.getDuel(id);
        assertEq(uint8(d.status), uint8(Colosseum.Status.Resolved));
        assertEq(d.winner, agentA);       // Alpha = higher PnL
        assertEq(d.shieldWinner, agentB);  // Iron Shield = higher resilience

        vm.expectRevert(Colosseum.DuelAlreadyResolved.selector);
        arena.resolve(id);
    }

    function test_resolve_tieGoesToA() public {
        uint256 id = _liveDuel();
        vm.startPrank(recorder);
        arena.reportCall(id, agentA, 0, 25, false, false, false);
        arena.reportCall(id, agentB, 0, 25, false, false, false);
        vm.stopPrank();
        vm.warp(block.timestamp + 1 hours + 1);
        arena.resolve(id);
        assertEq(arena.getDuel(id).winner, agentA);
    }

    function test_resolve_refundsStakesAndSplitsPrize() public {
        uint256 id = _liveDuel();
        // Seed a clean 100 USDC prize pool; A wins on PnL, no injections → no
        // shield winner, so the whole pool routes to the Alpha developer.
        vm.prank(alice);
        arena.fundPrizePool(id, 100 * USDC);
        vm.startPrank(recorder);
        arena.reportCall(id, agentA, 0, 100, false, false, false);
        arena.reportCall(id, agentB, 0, 10, false, false, false);
        vm.stopPrank();

        uint256 aBefore = usdc.balanceOf(devA);
        uint256 bBefore = usdc.balanceOf(devB);
        vm.warp(block.timestamp + 1 hours + 1);
        arena.resolve(id);

        // devA: 100 USDC prize (alpha + rolled shield half) + 50 USDC stake refund.
        assertEq(usdc.balanceOf(devA) - aBefore, 100 * USDC + STAKE);
        // devB: just the stake refund.
        assertEq(usdc.balanceOf(devB) - bBefore, STAKE);
        // Both agents de-registered.
        (, , , bool regA) = arena.agents(agentA);
        (, , , bool regB) = arena.agents(agentB);
        assertFalse(regA);
        assertFalse(regB);
    }

    function test_resolve_dualPrizeToTwoDevelopers() public {
        uint256 id = _liveDuel();
        vm.prank(recorder);
        arena.setOperatorCutBps(0); // simplify: full price escrows
        // A fooled (escrow → pool), B survives (bounty → devB). A alpha, B shield.
        vm.prank(alice);
        uint256 injA = arena.injectChaos(id, agentA, SHIELD); // 2 USDC escrow → pool on fooled
        vm.prank(bob);
        uint256 injB = arena.injectChaos(id, agentB, SHIELD); // 2 USDC → devB on survive
        vm.startPrank(recorder);
        arena.reportCall(id, agentA, injA, 100, true, false, false); // alpha, fooled
        arena.reportCall(id, agentB, injB, 10, true, true, false);   // shield, survived
        vm.stopPrank();

        uint256 aBefore = usdc.balanceOf(devA);
        uint256 bBefore = usdc.balanceOf(devB);
        vm.warp(block.timestamp + 1 hours + 1);
        arena.resolve(id);

        // Pool = 2 USDC (A's fooled escrow). Split 50/50: 1 USDC each.
        // devA = alpha 1 USDC + 50 USDC stake. devB = shield 1 USDC + 50 USDC stake
        //        + 2 USDC bounty (paid at reportCall, before these snapshots? no —
        //        snapshot is after reportCall, so bounty already in bBefore).
        assertEq(usdc.balanceOf(devA) - aBefore, 1 * USDC + STAKE);
        assertEq(usdc.balanceOf(devB) - bBefore, 1 * USDC + STAKE);
    }

    function test_resolve_forfeitsStakeOnChronicFailure() public {
        uint256 id = _liveDuel();
        // agentB fails the failureThreshold (default 3) times → stake forfeited.
        uint256 threshold = arena.failureThreshold();
        vm.startPrank(recorder);
        arena.reportCall(id, agentA, 0, 100, false, false, false);
        for (uint256 i = 0; i < threshold; i++) {
            arena.reportCall(id, agentB, 0, -10, false, false, true);
        }
        vm.stopPrank();

        uint256 aBefore = usdc.balanceOf(devA);
        uint256 bBefore = usdc.balanceOf(devB);
        vm.warp(block.timestamp + 1 hours + 1);
        arena.resolve(id);

        // No injections → no shield winner → whole pool (B's forfeited 50 USDC)
        // rolls to the Alpha winner devA, plus devA's own stake refund.
        assertEq(usdc.balanceOf(devA) - aBefore, STAKE + STAKE);
        // devB chronically failed → stake forfeited, no refund.
        assertEq(usdc.balanceOf(devB), bBefore);
    }

    // ---- claim (parimutuel payout) -----------------------------------

    function test_claim_parimutuelSplit() public {
        uint256 id = _bettingDuel();
        // poolA = 3 (alice 2, bob 1), poolB = 3 (carol). total = 6.
        vm.prank(alice);
        arena.bet(id, true, 2 * USDC);
        vm.prank(bob);
        arena.bet(id, true, 1 * USDC);
        vm.prank(carol);
        arena.bet(id, false, 3 * USDC);

        vm.prank(recorder);
        arena.reportCall(id, agentA, 0, 1, false, false, false); // A wins
        vm.warp(block.timestamp + 2 hours + 1); // past betting + trading
        arena.resolve(id); // refunds both stakes (no failures)

        uint256 aliceBefore = usdc.balanceOf(alice);
        vm.prank(alice);
        uint256 p = arena.claim(id); // 2 * 6 / 3 = 4
        assertEq(p, 4 * USDC);
        assertEq(usdc.balanceOf(alice) - aliceBefore, 4 * USDC);

        vm.prank(bob);
        assertEq(arena.claim(id), 2 * USDC); // 1 * 6 / 3 = 2

        // Loser (carol) has nothing to claim.
        vm.prank(carol);
        vm.expectRevert(Colosseum.NothingToClaim.selector);
        arena.claim(id);

        // Double-claim blocked.
        vm.prank(alice);
        vm.expectRevert(Colosseum.AlreadyClaimed.selector);
        arena.claim(id);

        // Bets + stakes fully distributed — nothing stranded.
        assertEq(usdc.balanceOf(address(arena)), 0);
    }

    function test_claim_sweepsRoundingDust() public {
        uint256 id = _bettingDuel();
        // Winning pool (A) = 1 + 2 = 3; losing pool (B) = 1. total = 4.
        // 1*4/3 and 2*4/3 both truncate → 1 base-unit of dust without the sweep.
        vm.prank(alice);
        arena.bet(id, true, 1 * USDC);
        vm.prank(bob);
        arena.bet(id, true, 2 * USDC);
        vm.prank(carol);
        arena.bet(id, false, 1 * USDC);

        vm.prank(recorder);
        arena.reportCall(id, agentA, 0, 1, false, false, false); // A wins
        vm.warp(block.timestamp + 2 hours + 1);
        arena.resolve(id);

        vm.prank(alice);
        uint256 pa = arena.claim(id);
        vm.prank(bob);
        uint256 pb = arena.claim(id);

        // Whole pot distributed — the last claimant swept the dust.
        assertEq(pa + pb, 4 * USDC);
        assertEq(usdc.balanceOf(address(arena)), 0);

        // Loser gets nothing.
        vm.prank(carol);
        vm.expectRevert(Colosseum.NothingToClaim.selector);
        arena.claim(id);
    }

    function test_claim_refundsWhenNoWinners() public {
        uint256 id = _bettingDuel();
        // Only carol bets, on B. Tie → winner A. winningPool(A)=0 → refund.
        vm.prank(carol);
        arena.bet(id, false, 5 * USDC);
        vm.warp(block.timestamp + 2 hours + 1);
        arena.resolve(id); // scoreA==scoreB==0 → A wins, poolA==0
        vm.prank(carol);
        assertEq(arena.claim(id), 5 * USDC); // own stake refunded
    }
}
