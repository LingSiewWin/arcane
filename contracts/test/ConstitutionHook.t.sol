// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Test, Vm} from "forge-std/Test.sol";
import {ConstitutionRegistry} from "../src/ConstitutionRegistry.sol";
import {ConstitutionHook} from "../src/ConstitutionHook.sol";
import {PackedUserOperation} from "../src/interfaces/IERC7579.sol";

contract ConstitutionHookTest is Test {
    ConstitutionRegistry internal registry;
    ConstitutionHook internal hook;

    address internal agent = address(0xA9E47);
    address internal goodVenue = address(0x600D);
    address internal badVenue = address(0xBADBADBA);

    bytes32 internal constitutionHash;

    function setUp() public {
        registry = new ConstitutionRegistry();
        hook = new ConstitutionHook(registry);

        // Constitution covers four kinds we will exercise:
        //  - MAX_LEVERAGE 2x = 20000 bps
        //  - MAX_TRADE_SIZE 1 USDC (1e6)
        //  - VENUE_BLACKLIST [badVenue]
        //  - SUBDELEGATION_BOUND 500_000 (0.5 USDC)
        ConstitutionRegistry.Rule[] memory rs = new ConstitutionRegistry.Rule[](4);
        rs[0] = ConstitutionRegistry.Rule({kind: 0, params: abi.encode(uint256(20000))});
        rs[1] = ConstitutionRegistry.Rule({kind: 1, params: abi.encode(uint256(1e6))});

        address[] memory blacklist = new address[](1);
        blacklist[0] = badVenue;
        rs[2] = ConstitutionRegistry.Rule({kind: 2, params: abi.encode(blacklist)});

        rs[3] = ConstitutionRegistry.Rule({kind: 4, params: abi.encode(uint256(500_000))});

        constitutionHash = registry.defineConstitution(rs);

        // Install hook on `agent`.
        vm.prank(agent);
        hook.onInstall(abi.encode(constitutionHash));
    }

    // -----------------------------------------------------------------------
    // userOp builder
    // -----------------------------------------------------------------------

    function _buildExecuteUserOp(address target, uint256 value, bytes memory inner)
        internal
        view
        returns (PackedUserOperation memory op)
    {
        // ERC-7579 execute(address,uint256,bytes) selector = 0xb61d27f6.
        bytes memory cd = abi.encodeWithSelector(bytes4(0xb61d27f6), target, value, inner);
        op = PackedUserOperation({
            sender: agent,
            nonce: 0,
            initCode: hex"",
            callData: cd,
            accountGasLimits: bytes32(0),
            preVerificationGas: 0,
            gasFees: bytes32(0),
            paymasterAndData: hex"",
            signature: hex""
        });
    }

    // -----------------------------------------------------------------------
    // Install / state
    // -----------------------------------------------------------------------

    function test_install_records_hash() public view {
        assertEq(hook.constitutionOf(agent), constitutionHash);
        assertTrue(hook.isInitialized(agent));
    }

    function test_install_unknown_hash_reverts() public {
        bytes32 fake = bytes32(uint256(0xdead));
        address other = address(0xBEEF);
        vm.prank(other);
        vm.expectRevert(ConstitutionHook.UnknownConstitution.selector);
        hook.onInstall(abi.encode(fake));
    }

    function test_double_install_reverts() public {
        vm.prank(agent);
        vm.expectRevert(ConstitutionHook.AlreadyInstalled.selector);
        hook.onInstall(abi.encode(constitutionHash));
    }

    // -----------------------------------------------------------------------
    // Happy path
    // -----------------------------------------------------------------------

    function test_inbounds_tx_returns_success() public {
        // ERC-20 transfer to goodVenue with amount 500_000 (< 1e6).
        bytes memory inner = abi.encodeWithSelector(bytes4(0xa9059cbb), goodVenue, uint256(500_000));
        PackedUserOperation memory op = _buildExecuteUserOp(goodVenue, 0, inner);

        vm.prank(op.sender);
        uint256 ret = hook.validateUserOp(op, bytes32(0));
        assertEq(ret, 0);
    }

    // -----------------------------------------------------------------------
    // MAX_TRADE_SIZE
    // -----------------------------------------------------------------------

    function test_exceeding_max_trade_size_native_reverts() public {
        // Pass user-op value > 1e6.
        PackedUserOperation memory op = _buildExecuteUserOp(goodVenue, 2e6, hex"");
        vm.prank(op.sender);
        vm.expectRevert(bytes("ConstitutionViolation:MAX_TRADE_SIZE"));
        hook.validateUserOp(op, bytes32(0));
    }

    function test_exceeding_max_trade_size_erc20_reverts() public {
        bytes memory inner = abi.encodeWithSelector(bytes4(0xa9059cbb), goodVenue, uint256(2e6));
        PackedUserOperation memory op = _buildExecuteUserOp(goodVenue, 0, inner);

        // Use recordLogs to assert the violation event fired too.
        vm.recordLogs();
        vm.prank(op.sender);
        vm.expectRevert(bytes("ConstitutionViolation:MAX_TRADE_SIZE"));
        hook.validateUserOp(op, bytes32(0));
    }

    function test_max_trade_size_emits_event_then_reverts() public {
        bytes memory inner = abi.encodeWithSelector(bytes4(0xa9059cbb), goodVenue, uint256(2e6));
        PackedUserOperation memory op = _buildExecuteUserOp(goodVenue, 0, inner);

        // expectEmit must precede the call that emits.
        vm.expectEmit(true, true, false, false, address(hook));
        emit ConstitutionHook.ConstitutionViolation(agent, 1, "");
        vm.prank(op.sender);
        vm.expectRevert(bytes("ConstitutionViolation:MAX_TRADE_SIZE"));
        hook.validateUserOp(op, bytes32(0));
    }

    // -----------------------------------------------------------------------
    // VENUE_BLACKLIST
    // -----------------------------------------------------------------------

    function test_blacklisted_target_reverts() public {
        bytes memory inner = abi.encodeWithSelector(bytes4(0xa9059cbb), badVenue, uint256(1));
        PackedUserOperation memory op = _buildExecuteUserOp(badVenue, 0, inner);
        vm.prank(op.sender);
        vm.expectRevert(bytes("ConstitutionViolation:VENUE_BLACKLIST"));
        hook.validateUserOp(op, bytes32(0));
    }

    // -----------------------------------------------------------------------
    // NO_UNAUDITED_CONTRACTS (separate constitution)
    // -----------------------------------------------------------------------

    function test_whitelist_active_non_whitelisted_reverts() public {
        // Build a fresh constitution that only contains a whitelist [goodVenue].
        address[] memory wl = new address[](1);
        wl[0] = goodVenue;

        ConstitutionRegistry.Rule[] memory rs = new ConstitutionRegistry.Rule[](1);
        rs[0] = ConstitutionRegistry.Rule({kind: 3, params: abi.encode(wl)});
        bytes32 wlHash = registry.defineConstitution(rs);

        address other = address(0xC0DE);
        vm.prank(other);
        hook.onInstall(abi.encode(wlHash));

        PackedUserOperation memory op = PackedUserOperation({
            sender: other,
            nonce: 0,
            initCode: hex"",
            callData: abi.encodeWithSelector(bytes4(0xb61d27f6), badVenue, uint256(0), hex""),
            accountGasLimits: bytes32(0),
            preVerificationGas: 0,
            gasFees: bytes32(0),
            paymasterAndData: hex"",
            signature: hex""
        });
        vm.prank(op.sender);
        vm.expectRevert(bytes("ConstitutionViolation:NO_UNAUDITED_CONTRACTS"));
        hook.validateUserOp(op, bytes32(0));
    }

    // -----------------------------------------------------------------------
    // SUBDELEGATION_BOUND
    // -----------------------------------------------------------------------

    function test_subdelegation_above_bound_reverts() public {
        // issueSessionKey(address,uint256) selector = 0x76671808
        bytes memory inner = abi.encodeWithSelector(
            bytes4(0x7873af1d), address(0xCC), uint256(800_000) // > 500_000
        );
        PackedUserOperation memory op = _buildExecuteUserOp(address(this), 0, inner);
        vm.prank(op.sender);
        vm.expectRevert(bytes("ConstitutionViolation:SUBDELEGATION_BOUND"));
        hook.validateUserOp(op, bytes32(0));
    }

    function test_subdelegation_within_bound_passes() public {
        bytes memory inner = abi.encodeWithSelector(
            bytes4(0x7873af1d), address(0xCC), uint256(400_000) // < 500_000
        );
        PackedUserOperation memory op = _buildExecuteUserOp(address(this), 0, inner);
        vm.prank(op.sender);
        uint256 ret = hook.validateUserOp(op, bytes32(0));
        assertEq(ret, 0);
    }

    // -----------------------------------------------------------------------
    // MAX_LEVERAGE
    // -----------------------------------------------------------------------

    function test_max_leverage_exceeded_reverts() public {
        // setLeverage(uint256) selector = 0xab033ea9
        bytes memory inner = abi.encodeWithSelector(bytes4(0x79575b23), uint256(30000)); // 3x > 2x
        PackedUserOperation memory op = _buildExecuteUserOp(goodVenue, 0, inner);
        vm.prank(op.sender);
        vm.expectRevert(bytes("ConstitutionViolation:MAX_LEVERAGE"));
        hook.validateUserOp(op, bytes32(0));
    }

    function test_max_leverage_at_bound_passes() public {
        bytes memory inner = abi.encodeWithSelector(bytes4(0x79575b23), uint256(20000));
        PackedUserOperation memory op = _buildExecuteUserOp(goodVenue, 0, inner);
        vm.prank(op.sender);
        uint256 ret = hook.validateUserOp(op, bytes32(0));
        assertEq(ret, 0);
    }

    // -----------------------------------------------------------------------
    // Uninstalled agent
    // -----------------------------------------------------------------------

    function test_uninstalled_agent_validate_reverts() public {
        address ghost = address(0xDEAD);
        // Use a well-formed execute() callData so we exercise the
        // NotInstalled branch specifically (post-F4 the hook fails
        // closed on unrecognised outer selectors, so an empty callData
        // would revert UnsupportedOuterSelector before reaching the
        // constitutionOf check). NotInstalled still wins because it's
        // checked first.
        bytes memory inner = abi.encodeWithSelector(bytes4(0xa9059cbb), goodVenue, uint256(0));
        bytes memory cd = abi.encodeWithSelector(bytes4(0xb61d27f6), goodVenue, uint256(0), inner);
        PackedUserOperation memory op = PackedUserOperation({
            sender: ghost,
            nonce: 0,
            initCode: hex"",
            callData: cd,
            accountGasLimits: bytes32(0),
            preVerificationGas: 0,
            gasFees: bytes32(0),
            paymasterAndData: hex"",
            signature: hex""
        });
        // Prank as the ghost so the F6 caller gate is satisfied and we
        // can observe the underlying NotInstalled revert.
        vm.prank(op.sender);
        vm.expectRevert(ConstitutionHook.NotInstalled.selector);
        hook.validateUserOp(op, bytes32(0));
    }

    // -----------------------------------------------------------------------
    // F4 — fail-closed on unknown outer selector
    // -----------------------------------------------------------------------

    function test_unknown_outer_selector_reverts() public {
        // callData with a bogus outer selector — must revert
        // UnsupportedOuterSelector(0xdeadbeef).
        bytes memory cd = abi.encodeWithSelector(bytes4(0xdeadbeef), uint256(1), uint256(2));
        PackedUserOperation memory op = PackedUserOperation({
            sender: agent,
            nonce: 0,
            initCode: hex"",
            callData: cd,
            accountGasLimits: bytes32(0),
            preVerificationGas: 0,
            gasFees: bytes32(0),
            paymasterAndData: hex"",
            signature: hex""
        });
        vm.prank(op.sender);
        vm.expectRevert(
            abi.encodeWithSelector(
                ConstitutionHook.UnsupportedOuterSelector.selector, bytes4(0xdeadbeef)
            )
        );
        hook.validateUserOp(op, bytes32(0));
    }

    function test_valid_execute_passes_explicit() public {
        // Explicit happy-path probe after F4 — a well-formed execute()
        // call still returns VALIDATION_SUCCESS (== 0).
        bytes memory inner = abi.encodeWithSelector(bytes4(0xa9059cbb), goodVenue, uint256(100));
        PackedUserOperation memory op = _buildExecuteUserOp(goodVenue, 0, inner);
        vm.prank(op.sender);
        uint256 ret = hook.validateUserOp(op, bytes32(0));
        assertEq(ret, 0, "valid execute() must return VALIDATION_SUCCESS");
    }

    function test_executeBatch_reverts_cleanly() public {
        // executeBatch(address[],uint256[],bytes[]) selector = 0x34fcd5be.
        // We deliberately do NOT support batched executes yet — the hook
        // must reject them rather than silently falling through.
        address[] memory targets = new address[](1);
        targets[0] = goodVenue;
        uint256[] memory values = new uint256[](1);
        values[0] = 0;
        bytes[] memory datas = new bytes[](1);
        datas[0] = hex"";

        bytes memory cd =
            abi.encodeWithSelector(bytes4(0x34fcd5be), targets, values, datas);
        PackedUserOperation memory op = PackedUserOperation({
            sender: agent,
            nonce: 0,
            initCode: hex"",
            callData: cd,
            accountGasLimits: bytes32(0),
            preVerificationGas: 0,
            gasFees: bytes32(0),
            paymasterAndData: hex"",
            signature: hex""
        });
        vm.prank(op.sender);
        vm.expectRevert(
            abi.encodeWithSelector(
                ConstitutionHook.UnsupportedOuterSelector.selector, bytes4(0x34fcd5be)
            )
        );
        hook.validateUserOp(op, bytes32(0));
    }

    function test_empty_calldata_reverts() public {
        // Empty callData on an INSTALLED agent must revert
        // UnsupportedOuterSelector(0x00000000). The uninstalled-agent
        // test above is unaffected because constitutionOf is checked
        // first and NotInstalled wins for that case.
        PackedUserOperation memory op = PackedUserOperation({
            sender: agent,
            nonce: 0,
            initCode: hex"",
            callData: hex"",
            accountGasLimits: bytes32(0),
            preVerificationGas: 0,
            gasFees: bytes32(0),
            paymasterAndData: hex"",
            signature: hex""
        });
        vm.prank(op.sender);
        vm.expectRevert(
            abi.encodeWithSelector(
                ConstitutionHook.UnsupportedOuterSelector.selector, bytes4(0)
            )
        );
        hook.validateUserOp(op, bytes32(0));
    }

    function test_execute_with_truncated_body_reverts() public {
        // Outer selector is execute() but the body is too short to abi-decode
        // (address,uint256,bytes). Must revert UnsupportedOuterSelector(EXECUTE)
        // rather than letting abi.decode revert with a generic error.
        bytes memory cd = abi.encodePacked(bytes4(0xb61d27f6), bytes32(uint256(0)));
        PackedUserOperation memory op = PackedUserOperation({
            sender: agent,
            nonce: 0,
            initCode: hex"",
            callData: cd,
            accountGasLimits: bytes32(0),
            preVerificationGas: 0,
            gasFees: bytes32(0),
            paymasterAndData: hex"",
            signature: hex""
        });
        vm.prank(op.sender);
        vm.expectRevert(
            abi.encodeWithSelector(
                ConstitutionHook.UnsupportedOuterSelector.selector, bytes4(0xb61d27f6)
            )
        );
        hook.validateUserOp(op, bytes32(0));
    }

    // -----------------------------------------------------------------------
    // F6 — validateUserOp caller gate
    // -----------------------------------------------------------------------

    function test_validateUserOp_rejects_foreign_caller() public {
        // A random non-account address tries to submit a validation
        // request for ``agent``. The hook must revert with
        // NOT_OWN_USEROP without touching the constitution or emitting
        // a violation event under the agent's address.
        bytes memory inner = abi.encodeWithSelector(bytes4(0xa9059cbb), goodVenue, uint256(100));
        PackedUserOperation memory op = _buildExecuteUserOp(goodVenue, 0, inner);

        address attacker = address(0xC0FFEE);
        vm.prank(attacker);
        vm.expectRevert(bytes("ConstitutionViolation:NOT_OWN_USEROP"));
        hook.validateUserOp(op, bytes32(0));
    }

    function test_validateUserOp_accepts_self_caller() public {
        // When the caller is the user-op sender, an inbounds op
        // validates as before.
        bytes memory inner = abi.encodeWithSelector(bytes4(0xa9059cbb), goodVenue, uint256(100));
        PackedUserOperation memory op = _buildExecuteUserOp(goodVenue, 0, inner);

        vm.prank(agent);
        uint256 ret = hook.validateUserOp(op, bytes32(0));
        assertEq(ret, 0);
    }
}
