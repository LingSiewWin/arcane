// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Test} from "forge-std/Test.sol";
import {ConstitutionRegistry} from "../src/ConstitutionRegistry.sol";

contract ConstitutionRegistryTest is Test {
    ConstitutionRegistry internal registry;

    function setUp() public {
        registry = new ConstitutionRegistry();
    }

    function _twoRules() internal pure returns (ConstitutionRegistry.Rule[] memory rs) {
        rs = new ConstitutionRegistry.Rule[](2);
        rs[0] = ConstitutionRegistry.Rule({kind: 0, params: abi.encode(uint256(20000))});
        rs[1] = ConstitutionRegistry.Rule({kind: 1, params: abi.encode(uint256(1_000_000))});
    }

    function _differentRules() internal pure returns (ConstitutionRegistry.Rule[] memory rs) {
        rs = new ConstitutionRegistry.Rule[](1);
        rs[0] = ConstitutionRegistry.Rule({kind: 2, params: abi.encode(new address[](0))});
    }

    function test_define_stores_and_returns_rules() public {
        ConstitutionRegistry.Rule[] memory rs = _twoRules();
        bytes32 h = registry.defineConstitution(rs);

        assertTrue(registry.exists(h));
        assertEq(registry.ruleCount(h), 2);

        ConstitutionRegistry.Rule[] memory back = registry.getConstitution(h);
        assertEq(back.length, 2);
        assertEq(back[0].kind, 0);
        assertEq(back[1].kind, 1);
        assertEq(abi.decode(back[0].params, (uint256)), 20000);
        assertEq(abi.decode(back[1].params, (uint256)), 1_000_000);
    }

    function test_same_rules_same_hash() public {
        bytes32 h1 = registry.defineConstitution(_twoRules());
        bytes32 h2 = registry.defineConstitution(_twoRules());
        assertEq(h1, h2);
        // Defining twice is a no-op for storage but still produces same id.
        assertEq(registry.ruleCount(h1), 2);
    }

    function test_different_rules_different_hash() public {
        bytes32 h1 = registry.defineConstitution(_twoRules());
        bytes32 h2 = registry.defineConstitution(_differentRules());
        assertTrue(h1 != h2);
    }

    function test_empty_rules_revert() public {
        ConstitutionRegistry.Rule[] memory rs = new ConstitutionRegistry.Rule[](0);
        vm.expectRevert(ConstitutionRegistry.EmptyConstitution.selector);
        registry.defineConstitution(rs);
    }

    function test_get_unknown_reverts() public {
        bytes32 fake = bytes32(uint256(1));
        vm.expectRevert(abi.encodeWithSelector(ConstitutionRegistry.UnknownConstitution.selector, fake));
        registry.getConstitution(fake);
    }
}
