// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Test} from "forge-std/Test.sol";
import {MemoryAnchor} from "../src/MemoryAnchor.sol";

/// @dev Minimal ERC-721 stand-in used to exercise the identity-bound anchor
///      path. Only implements what MemoryAnchor consumes: ``ownerOf``. Mint
///      is `external` so tests can prank identities into existence cheaply.
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

contract MemoryAnchorTest is Test {
    MemoryAnchor internal anchor;
    MockERC721 internal registry;
    address internal agent = address(0xA9E47);
    uint256 internal constant ALICE_ID = 42;

    event MemoryAnchored(
        address indexed agent,
        uint256 indexed identityId,
        bytes32 root,
        uint256 timestamp
    );

    function setUp() public {
        registry = new MockERC721();
        anchor = new MemoryAnchor(address(registry));
    }

    // ---------------------------------------------------------------
    // Identity-bound anchor path (the F10 fix)
    // ---------------------------------------------------------------

    function test_anchor_with_identity_emits_event_and_stores() public {
        registry.mint(agent, ALICE_ID);
        bytes32 root = keccak256("hello");

        vm.expectEmit(true, true, false, true, address(anchor));
        emit MemoryAnchored(agent, ALICE_ID, root, block.timestamp);

        vm.prank(agent);
        anchor.anchor(ALICE_ID, root);

        assertEq(anchor.rootOfIdentity(ALICE_ID), root);
        assertEq(anchor.countByIdentity(ALICE_ID), 1);
    }

    function test_anchor_reverts_if_not_identity_owner() public {
        // ALICE_ID is owned by `agent`, but `attacker` tries to anchor.
        registry.mint(agent, ALICE_ID);
        address attacker = address(0xBADBAD);
        bytes32 root = keccak256("steal");

        vm.prank(attacker);
        vm.expectRevert(MemoryAnchor.NotIdentityOwner.selector);
        anchor.anchor(ALICE_ID, root);
    }

    function test_anchor_reverts_on_empty_root_identity_path() public {
        registry.mint(agent, ALICE_ID);
        vm.prank(agent);
        vm.expectRevert(MemoryAnchor.EmptyRoot.selector);
        anchor.anchor(ALICE_ID, bytes32(0));
    }

    function test_anchor_per_identity_isolated() public {
        address a1 = address(0x1111);
        address a2 = address(0x2222);
        uint256 id1 = 1;
        uint256 id2 = 2;
        registry.mint(a1, id1);
        registry.mint(a2, id2);

        bytes32 r1 = keccak256("agent1");
        bytes32 r2 = keccak256("agent2");

        vm.prank(a1);
        anchor.anchor(id1, r1);
        vm.prank(a2);
        anchor.anchor(id2, r2);

        assertEq(anchor.rootOfIdentity(id1), r1);
        assertEq(anchor.rootOfIdentity(id2), r2);
        assertEq(anchor.countByIdentity(id1), 1);
        assertEq(anchor.countByIdentity(id2), 1);
    }

    function test_anchor_identity_sequence_increments() public {
        registry.mint(agent, ALICE_ID);
        bytes32 r1 = keccak256("v1");
        bytes32 r2 = keccak256("v2");

        vm.startPrank(agent);
        anchor.anchor(ALICE_ID, r1);
        skip(60);
        anchor.anchor(ALICE_ID, r2);
        vm.stopPrank();

        assertEq(anchor.rootOfIdentity(ALICE_ID), r2);
        assertEq(anchor.countByIdentity(ALICE_ID), 2);
    }

    function test_anchor_reverts_if_identity_does_not_exist() public {
        // No mint for id=999 → registry.ownerOf reverts → bubbles up.
        bytes32 root = keccak256("ghost");
        vm.prank(agent);
        vm.expectRevert();
        anchor.anchor(999, root);
    }

    // ---------------------------------------------------------------
    // Legacy address-keyed path (backward compat — identityId = 0)
    // ---------------------------------------------------------------

    function test_anchor_by_address_legacy_emits_with_zero_identity() public {
        bytes32 root = keccak256("legacy");

        vm.expectEmit(true, true, false, true, address(anchor));
        emit MemoryAnchored(agent, 0, root, block.timestamp);

        vm.prank(agent);
        anchor.anchor(root); // legacy selector `anchor(bytes32)`

        assertEq(anchor.rootOf(agent), root);
        assertEq(anchor.count(agent), 1);
    }

    function test_anchor_by_address_alias_works() public {
        // The spec-named alias must reach the same legacy path.
        bytes32 root = keccak256("alias");

        vm.expectEmit(true, true, false, true, address(anchor));
        emit MemoryAnchored(agent, 0, root, block.timestamp);

        vm.prank(agent);
        anchor.anchorByAddress(root);

        assertEq(anchor.rootOf(agent), root);
        assertEq(anchor.count(agent), 1);
    }

    function test_anchor_legacy_per_agent_isolated() public {
        bytes32 r1 = keccak256("agent1");
        bytes32 r2 = keccak256("agent2");
        address a1 = address(0x1);
        address a2 = address(0x2);

        vm.prank(a1);
        anchor.anchor(r1);
        vm.prank(a2);
        anchor.anchor(r2);

        assertEq(anchor.rootOf(a1), r1);
        assertEq(anchor.rootOf(a2), r2);
        assertEq(anchor.count(a1), 1);
        assertEq(anchor.count(a2), 1);
    }

    function test_anchor_legacy_sequence_increments() public {
        bytes32 r1 = keccak256("v1");
        bytes32 r2 = keccak256("v2");

        vm.startPrank(agent);
        anchor.anchor(r1);
        skip(60);
        anchor.anchor(r2);
        vm.stopPrank();

        assertEq(anchor.rootOf(agent), r2);
        assertEq(anchor.count(agent), 2);
    }

    function test_empty_root_reverts() public {
        vm.prank(agent);
        vm.expectRevert(MemoryAnchor.EmptyRoot.selector);
        anchor.anchor(bytes32(0));
    }

    function test_identity_registry_is_set() public view {
        assertEq(anchor.identityRegistry(), address(registry));
    }

    // ---------------------------------------------------------------
    // Gas check (identity path)
    // ---------------------------------------------------------------

    function test_gas_for_anchor() public {
        registry.mint(agent, ALICE_ID);
        bytes32 r = keccak256("gas-check");
        vm.prank(agent);
        uint256 gasBefore = gasleft();
        anchor.anchor(ALICE_ID, r);
        uint256 gasUsed = gasBefore - gasleft();
        // Identity-bound anchor: two SSTOREs + an event + one external
        // ownerOf SLOAD. Loose sanity bound; still well under 100k.
        assertLt(gasUsed, 100_000);
    }
}
