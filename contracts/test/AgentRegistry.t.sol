// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {AgentRegistry} from "../src/AgentRegistry.sol";
import {BondVault} from "../src/BondVault.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

/// @dev Minimal ERC-721 stand-in for the ERC-8004 identity registry. Only
///      implements `ownerOf` (what AgentRegistry consumes). Mirrors the mock
///      already used by MemoryAnchor.t.sol.
contract MockERC721 {
    mapping(uint256 => address) private _owners;

    error NonexistentToken();

    function mint(address to, uint256 id) external {
        _owners[id] = to;
    }

    function ownerOf(uint256 id) external view returns (address) {
        address o = _owners[id];
        if (o == address(0)) revert NonexistentToken();
        return o;
    }
}

contract AgentRegistryTest is Test {
    AgentRegistry internal reg;
    MockERC721 internal identity;
    BondVault internal vault;
    MockERC20 internal usdc;

    address internal alice = address(0xA11CE);
    address internal bob = address(0xB0B);

    uint256 internal constant ALICE_ID = 42;
    uint256 internal constant BOB_ID = 7;
    uint256 internal constant ONE_USDC = 1e6;
    bytes32 internal constant CONSTITUTION = keccak256("alice-constitution");
    string internal constant DARK_POOL = "https://alice.darkpool.example";

    event AgentRegistered(
        uint256 indexed agentId,
        uint256 indexed identityId,
        address indexed operator,
        bytes32 constitutionHash
    );
    event AgentAction(
        uint256 indexed agentId,
        uint8 indexed kind,
        bytes payload,
        uint256 timestamp
    );
    event AgentDeactivated(uint256 indexed agentId);

    function setUp() public {
        identity = new MockERC721();
        usdc = new MockERC20(6);
        // Real BondVault — `register` checks the real `balanceOf`.
        vault = new BondVault(IERC20(address(usdc)), address(0xBEEF), 7 days, 3 days);
        reg = new AgentRegistry(address(identity));
    }

    /// @dev Fund `who` and post a real bond so balanceOf(who) > 0.
    function _postBond(address who, uint256 amount) internal {
        usdc.mint(who, amount);
        vm.startPrank(who);
        usdc.approve(address(vault), type(uint256).max);
        vault.post(amount);
        vm.stopPrank();
    }

    // -----------------------------------------------------------------------
    // register
    // -----------------------------------------------------------------------

    function test_register_succeeds_when_owner_and_bonded() public {
        identity.mint(alice, ALICE_ID);
        _postBond(alice, 2 * ONE_USDC);

        vm.expectEmit(true, true, true, true, address(reg));
        emit AgentRegistered(1, ALICE_ID, alice, CONSTITUTION);

        vm.prank(alice);
        uint256 agentId = reg.register(ALICE_ID, CONSTITUTION, address(vault), DARK_POOL);

        assertEq(agentId, 1, "first agentId is 1");
        assertEq(reg.agentCount(), 1);
        assertEq(reg.agentByIdentity(ALICE_ID), 1);

        AgentRegistry.Agent memory a = reg.getAgent(1);
        assertEq(a.identityId, ALICE_ID);
        assertEq(a.constitutionHash, CONSTITUTION);
        assertEq(a.bondVault, address(vault));
        assertEq(a.darkPoolUrl, DARK_POOL);
        assertEq(a.operator, alice);
        assertEq(a.registeredAt, uint64(block.timestamp));
        assertTrue(a.active);
    }

    function test_register_reverts_if_not_identity_owner() public {
        // ALICE_ID is owned by alice; bob (also bonded) tries to register it.
        identity.mint(alice, ALICE_ID);
        _postBond(bob, ONE_USDC);

        vm.prank(bob);
        vm.expectRevert(AgentRegistry.NotIdentityOwner.selector);
        reg.register(ALICE_ID, CONSTITUTION, address(vault), DARK_POOL);
    }

    function test_register_reverts_if_no_bond() public {
        // alice owns the identity but has never posted a bond.
        identity.mint(alice, ALICE_ID);

        vm.prank(alice);
        vm.expectRevert(AgentRegistry.NoBond.selector);
        reg.register(ALICE_ID, CONSTITUTION, address(vault), DARK_POOL);
    }

    function test_register_reverts_on_duplicate_identity() public {
        identity.mint(alice, ALICE_ID);
        _postBond(alice, 2 * ONE_USDC);

        vm.prank(alice);
        reg.register(ALICE_ID, CONSTITUTION, address(vault), DARK_POOL);

        // Second registration of the same identity reverts.
        vm.prank(alice);
        vm.expectRevert(AgentRegistry.IdentityAlreadyRegistered.selector);
        reg.register(ALICE_ID, CONSTITUTION, address(vault), DARK_POOL);
    }

    // -----------------------------------------------------------------------
    // recordAction
    // -----------------------------------------------------------------------

    function test_recordAction_only_operator() public {
        identity.mint(alice, ALICE_ID);
        _postBond(alice, ONE_USDC);
        vm.prank(alice);
        uint256 agentId = reg.register(ALICE_ID, CONSTITUTION, address(vault), DARK_POOL);

        bytes memory payload = abi.encode(keccak256("advice-trace"));

        // Non-operator cannot record.
        vm.prank(bob);
        vm.expectRevert(AgentRegistry.NotAgentOperator.selector);
        reg.recordAction(agentId, 0, payload);

        // Operator records an ADVICE_PUBLISHED (kind=0) action; event fires
        // with the correct kind + payload.
        vm.expectEmit(true, true, false, true, address(reg));
        emit AgentAction(agentId, 0, payload, block.timestamp);
        vm.prank(alice);
        reg.recordAction(agentId, 0, payload);
    }

    // -----------------------------------------------------------------------
    // agentByIdentity + agentCount across multiple agents
    // -----------------------------------------------------------------------

    function test_agentByIdentity_and_count() public {
        identity.mint(alice, ALICE_ID);
        identity.mint(bob, BOB_ID);
        _postBond(alice, ONE_USDC);
        _postBond(bob, ONE_USDC);

        vm.prank(alice);
        uint256 aliceAgent = reg.register(ALICE_ID, CONSTITUTION, address(vault), DARK_POOL);
        vm.prank(bob);
        uint256 bobAgent = reg.register(BOB_ID, keccak256("bob-c"), address(vault), "https://bob.example");

        assertEq(aliceAgent, 1);
        assertEq(bobAgent, 2);
        assertEq(reg.agentCount(), 2);
        assertEq(reg.agentByIdentity(ALICE_ID), 1);
        assertEq(reg.agentByIdentity(BOB_ID), 2);
        // Unregistered identity returns 0.
        assertEq(reg.agentByIdentity(99999), 0);

        assertEq(reg.getAgent(1).operator, alice);
        assertEq(reg.getAgent(2).operator, bob);
    }

    // -----------------------------------------------------------------------
    // deactivate
    // -----------------------------------------------------------------------

    function test_deactivate_only_operator() public {
        identity.mint(alice, ALICE_ID);
        _postBond(alice, ONE_USDC);
        vm.prank(alice);
        uint256 agentId = reg.register(ALICE_ID, CONSTITUTION, address(vault), DARK_POOL);

        assertTrue(reg.getAgent(agentId).active);

        // Non-operator cannot deactivate.
        vm.prank(bob);
        vm.expectRevert(AgentRegistry.NotAgentOperator.selector);
        reg.deactivate(agentId);

        // Operator deactivates; active flag flips and event fires.
        vm.expectEmit(true, false, false, false, address(reg));
        emit AgentDeactivated(agentId);
        vm.prank(alice);
        reg.deactivate(agentId);

        assertFalse(reg.getAgent(agentId).active);
    }
}
