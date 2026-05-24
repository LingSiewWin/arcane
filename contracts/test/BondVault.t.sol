// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {BondVault} from "../src/BondVault.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract BondVaultTest is Test {
    MockERC20 internal usdc;
    BondVault internal vault;

    address internal owner = address(this);
    address internal oracle = address(0x0BCDE);
    address internal alice = address(0xA11CE);
    // Canonical Erasure burn sink — both bond and counter-bond go here.
    address internal constant BURN = 0x000000000000000000000000000000000000dEaD;

    uint256 internal constant ONE_USDC = 1e6;
    uint256 internal constant WINDOW = 7 days;
    uint256 internal constant LIVENESS = 3 days;

    function setUp() public {
        usdc = new MockERC20(6);
        vault = new BondVault(IERC20(address(usdc)), oracle, WINDOW, LIVENESS);

        usdc.mint(alice, 10 * ONE_USDC);
        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);

        // The oracle must fund + post a counter-bond before it can slash
        // (Erasure double-burn). Give it allowance + balance up front.
        usdc.mint(oracle, 10 * ONE_USDC);
        vm.prank(oracle);
        usdc.approve(address(vault), type(uint256).max);
    }

    // -----------------------------------------------------------------------
    // Post
    // -----------------------------------------------------------------------

    function test_post_pulls_funds_and_increments_balance() public {
        vm.prank(alice);
        vault.post(2 * ONE_USDC);
        assertEq(vault.balanceOf(alice), 2 * ONE_USDC);
        assertEq(usdc.balanceOf(address(vault)), 2 * ONE_USDC);
        assertEq(usdc.balanceOf(alice), 8 * ONE_USDC);
    }

    function test_post_zero_reverts() public {
        vm.prank(alice);
        vm.expectRevert(BondVault.ZeroAmount.selector);
        vault.post(0);
    }

    // -----------------------------------------------------------------------
    // Slash
    // -----------------------------------------------------------------------

    function test_slash_burns_both_legs_to_dead_address() public {
        // Erasure double-burn: slashing destroys the agent's bond AND an
        // equal counter-bond from the slasher. Neither the oracle nor any
        // insurer profits — both legs land at 0x…dEaD.
        vm.prank(alice);
        vault.post(2 * ONE_USDC);

        // Oracle must post a counter-bond >= slash amount first.
        vm.prank(oracle);
        vault.postCounterBond(ONE_USDC);

        uint256 burnBefore = usdc.balanceOf(BURN);

        vm.prank(oracle);
        vault.slash(alice, ONE_USDC);

        assertEq(vault.balanceOf(alice), ONE_USDC, "agent bond reduced by slash");
        // Both legs burned: agent's 1 USDC + oracle's 1 USDC counter-bond.
        assertEq(usdc.balanceOf(BURN) - burnBefore, 2 * ONE_USDC, "both legs burned");
        assertEq(vault.counterBonds(oracle), 0, "counter-bond consumed");
    }

    function test_slash_without_counter_bond_reverts() public {
        vm.prank(alice);
        vault.post(2 * ONE_USDC);
        // Oracle has NOT posted a counter-bond — slash must revert.
        vm.prank(oracle);
        vm.expectRevert(BondVault.CounterBondLessThanSlash.selector);
        vault.slash(alice, ONE_USDC);
    }

    function test_slash_by_non_oracle_reverts() public {
        vm.prank(alice);
        vault.post(2 * ONE_USDC);

        vm.expectRevert(BondVault.NotOracle.selector);
        vault.slash(alice, ONE_USDC);
    }

    function test_slash_more_than_bond_reverts() public {
        vm.prank(alice);
        vault.post(ONE_USDC);

        vm.prank(oracle);
        vm.expectRevert(BondVault.InsufficientBond.selector);
        vault.slash(alice, 2 * ONE_USDC);
    }

    function test_slash_zero_reverts() public {
        vm.prank(alice);
        vault.post(ONE_USDC);
        vm.prank(oracle);
        vm.expectRevert(BondVault.ZeroAmount.selector);
        vault.slash(alice, 0);
    }

    // -----------------------------------------------------------------------
    // Release
    // -----------------------------------------------------------------------

    function test_release_after_window_succeeds() public {
        vm.prank(alice);
        vault.post(2 * ONE_USDC);

        skip(WINDOW + 1);

        vm.prank(alice);
        vault.release();
        assertEq(vault.balanceOf(alice), 0);
        assertEq(usdc.balanceOf(alice), 10 * ONE_USDC);
    }

    function test_release_before_window_reverts() public {
        vm.prank(alice);
        vault.post(2 * ONE_USDC);

        // Skip _almost_ the whole window so the readyAt timestamp is precise.
        skip(WINDOW - 1);
        uint256 readyAt = vault.readyAt(alice);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(BondVault.ReleaseTooEarly.selector, readyAt));
        vault.release();
    }

    function test_release_after_oracle_approval_succeeds() public {
        vm.prank(alice);
        vault.post(2 * ONE_USDC);

        vm.prank(oracle);
        vault.approveRelease(alice);

        vm.prank(alice);
        vault.release();
        assertEq(vault.balanceOf(alice), 0);
    }

    function test_release_no_bond_reverts() public {
        vm.prank(alice);
        vm.expectRevert(BondVault.NoBond.selector);
        vault.release();
    }

    // -----------------------------------------------------------------------
    // Admin
    // -----------------------------------------------------------------------

    function test_only_owner_can_set_oracle() public {
        address attacker = address(0x666);
        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, attacker));
        vault.setOracle(attacker);

        // Owner can update.
        vault.setOracle(address(0xCAFE));
        assertEq(vault.oracle(), address(0xCAFE));
    }

    // (Removed test_set_insurance_zero_reverts — the Erasure double-burn
    // BondVault has no insurance pool; slashed funds burn to 0x…dEaD, so
    // setInsurance no longer exists.)

    function test_set_window() public {
        vault.setReleaseWindow(123);
        assertEq(vault.releaseWindow(), 123);
    }

    // -----------------------------------------------------------------------
    // releaseToOperator (dead-agent rescue — must pay the funder, not a caller)
    // -----------------------------------------------------------------------

    function test_releaseToOperator_pays_funder_not_caller() public {
        vm.prank(alice);
        vault.post(2 * ONE_USDC);

        // Agent goes quiet past the liveness timeout (no checker → time-only).
        skip(LIVENESS + 1);
        assertFalse(vault.isAgentAlive(alice));

        // A random third party triggers the rescue. The bond returns to the
        // funder (alice), never to the caller — the old caller-supplied
        // recipient theft vector is gone (signature no longer accepts one).
        address stranger = address(0xBADBAD);
        uint256 aliceBefore = usdc.balanceOf(alice);
        vm.prank(stranger);
        vault.releaseToOperator(alice);

        assertEq(usdc.balanceOf(alice) - aliceBefore, 2 * ONE_USDC);
        assertEq(usdc.balanceOf(stranger), 0);
        assertEq(vault.balanceOf(alice), 0);
    }

    function test_releaseToOperator_reverts_while_alive() public {
        vm.prank(alice);
        vault.post(2 * ONE_USDC);
        vm.expectRevert(BondVault.AgentStillAlive.selector);
        vault.releaseToOperator(alice);
    }
}
