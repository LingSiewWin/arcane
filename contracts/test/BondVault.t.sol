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
    address internal insurance = address(0x10003);
    address internal alice = address(0xA11CE);

    uint256 internal constant ONE_USDC = 1e6;
    uint256 internal constant WINDOW = 7 days;

    function setUp() public {
        usdc = new MockERC20(6);
        vault = new BondVault(IERC20(address(usdc)), oracle, insurance, WINDOW);

        usdc.mint(alice, 10 * ONE_USDC);
        vm.prank(alice);
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

    function test_slash_by_oracle_moves_funds_to_insurance() public {
        vm.prank(alice);
        vault.post(2 * ONE_USDC);

        vm.prank(oracle);
        vault.slash(alice, ONE_USDC);

        assertEq(vault.balanceOf(alice), ONE_USDC);
        assertEq(usdc.balanceOf(insurance), ONE_USDC);
        assertEq(usdc.balanceOf(address(vault)), ONE_USDC);
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

    function test_set_insurance_zero_reverts() public {
        vm.expectRevert(BondVault.InvalidAddress.selector);
        vault.setInsurance(address(0));
    }

    function test_set_window() public {
        vault.setReleaseWindow(123);
        assertEq(vault.releaseWindow(), 123);
    }
}
