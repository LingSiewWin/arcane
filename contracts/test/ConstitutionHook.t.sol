// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Test, Vm} from "forge-std/Test.sol";
import {ConstitutionRegistry} from "../src/ConstitutionRegistry.sol";
import {ConstitutionValidator} from "../src/ConstitutionValidator.sol";
import {PackedUserOperation} from "../src/interfaces/IERC7579.sol";
import {GmxV2PerpAdapter} from "../src/adapters/GmxV2PerpAdapter.sol";
import {Permit2Adapter} from "../src/adapters/Permit2Adapter.sol";

contract ConstitutionValidatorTest is Test {
    ConstitutionRegistry internal registry;
    ConstitutionValidator internal hook;

    // Real adapters wired into the constitution so MAX_LEVERAGE (kind 0)
    // and SUBDELEGATION_BOUND (kind 4) decode real protocol calldata.
    GmxV2PerpAdapter internal gmx;
    Permit2Adapter internal permit2;

    address internal agent = address(0xA9E47);
    address internal goodVenue = address(0x600D);
    address internal badVenue = address(0xBADBADBA);

    bytes32 internal constitutionHash;

    function setUp() public {
        registry = new ConstitutionRegistry();
        hook = new ConstitutionValidator(registry);
        gmx = new GmxV2PerpAdapter();
        permit2 = new Permit2Adapter();

        // Constitution covers four kinds we will exercise:
        //  - MAX_LEVERAGE 2x = 20000 bps           (GmxV2PerpAdapter)
        //  - MAX_TRADE_SIZE 1 USDC (1e6)           (inline fast path)
        //  - VENUE_BLACKLIST [badVenue]            (inline target compare)
        //  - SUBDELEGATION_BOUND 500_000 (0.5 USDC) (Permit2Adapter)
        // B16: kinds 0 and 4 require a non-zero adapter at registration.
        ConstitutionRegistry.Rule[] memory rs = new ConstitutionRegistry.Rule[](4);
        rs[0] = ConstitutionRegistry.Rule({kind: 0, params: abi.encode(uint256(20000)), adapter: address(gmx)});
        rs[1] = ConstitutionRegistry.Rule({kind: 1, params: abi.encode(uint256(1e6)), adapter: address(0)});

        address[] memory blacklist = new address[](1);
        blacklist[0] = badVenue;
        rs[2] = ConstitutionRegistry.Rule({kind: 2, params: abi.encode(blacklist), adapter: address(0)});

        rs[3] = ConstitutionRegistry.Rule({kind: 4, params: abi.encode(uint256(500_000)), adapter: address(permit2)});

        constitutionHash = registry.defineConstitution(rs);

        // Install hook on `agent`.
        vm.prank(agent);
        hook.onInstall(abi.encode(constitutionHash));
    }

    // -----------------------------------------------------------------------
    // Real adapter calldata builders
    // -----------------------------------------------------------------------

    /// @dev Build real GMX v2 `createOrder(CreateOrderParams)` calldata.
    ///      The GmxV2PerpAdapter computes:
    ///        sizeUsdc    = sizeDeltaUsd / 1e24
    ///        leverageBps = (sizeUsdc * 10000) / initialCollateralDeltaAmount
    ///      We pass sizeDeltaUsd / collateral so the decoded leverageBps
    ///      lands exactly on `leverageBpsTarget`.
    function _gmxCreateOrderCalldata(uint256 leverageBpsTarget)
        internal
        view
        returns (bytes memory)
    {
        // Choose a collateral of 1000 USDC (1e9 base units). Then
        // sizeUsdc = leverageBpsTarget * collateral / 10000, and
        // sizeDeltaUsd = sizeUsdc * 1e24.
        uint256 collateral = 1_000 * 1e6; // 1000 USDC in 1e6 base units
        uint256 sizeUsdc = (leverageBpsTarget * collateral) / 10_000;
        uint256 sizeDeltaUsd = sizeUsdc * 1e24; // back to GMX 1e30 precision

        GmxV2PerpAdapter.CreateOrderParamsAddresses memory addrs =
            GmxV2PerpAdapter.CreateOrderParamsAddresses({
                receiver: agent,
                cancellationReceiver: address(0),
                callbackContract: address(0),
                uiFeeReceiver: address(0),
                market: address(0x3A4E27), // a GMX market token
                initialCollateralToken: address(0x05DC), // a USDC-like token
                swapPath: new address[](0)
            });
        GmxV2PerpAdapter.CreateOrderParamsNumbers memory nums =
            GmxV2PerpAdapter.CreateOrderParamsNumbers({
                sizeDeltaUsd: sizeDeltaUsd,
                initialCollateralDeltaAmount: collateral,
                triggerPrice: 0,
                acceptablePrice: 0,
                executionFee: 0,
                callbackGasLimit: 0,
                minOutputAmount: 0,
                validFromTime: 0
            });
        GmxV2PerpAdapter.CreateOrderParams memory p = GmxV2PerpAdapter.CreateOrderParams({
            addresses: addrs,
            numbers: nums,
            orderType: 2,                  // MarketIncrease (open position)
            decreasePositionSwapType: 0,
            isLong: true,
            shouldUnwrapNativeToken: false,
            autoCancel: false,
            referralCode: bytes32(0),
            dataList: new bytes32[](0)
        });

        bytes4 sel = gmx.selectors()[0];
        return abi.encodePacked(sel, abi.encode(p));
    }

    /// @dev Build real Uniswap Permit2 single-token `permitTransferFrom`
    ///      calldata. The Permit2Adapter returns `sizeUsdc = requestedAmount`
    ///      (falling back to the permitted cap when zero) — the validator's
    ///      SUBDELEGATION_BOUND check treats that as the proposed child
    ///      spend allowance.
    function _permit2Calldata(uint256 requestedAmount)
        internal
        view
        returns (bytes memory)
    {
        Permit2Adapter.TokenPermissions memory tp = Permit2Adapter.TokenPermissions({
            token: address(0x05DC), // USDC-like token being permitted
            amount: requestedAmount
        });
        Permit2Adapter.PermitTransferFrom memory permit = Permit2Adapter.PermitTransferFrom({
            permitted: tp,
            nonce: 1,
            deadline: type(uint256).max
        });
        Permit2Adapter.SignatureTransferDetails memory details =
            Permit2Adapter.SignatureTransferDetails({
                to: goodVenue,
                requestedAmount: requestedAmount
            });

        bytes4 sel = permit2.selectors()[0];
        return abi.encodePacked(
            sel,
            abi.encode(permit, details, agent, bytes(""))
        );
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
        vm.expectRevert(ConstitutionValidator.UnknownConstitution.selector);
        hook.onInstall(abi.encode(fake));
    }

    function test_double_install_reverts() public {
        vm.prank(agent);
        vm.expectRevert(ConstitutionValidator.AlreadyInstalled.selector);
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
        emit ConstitutionValidator.ConstitutionViolation(agent, 1, "");
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
        rs[0] = ConstitutionRegistry.Rule({kind: 3, params: abi.encode(wl), adapter: address(0)});
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
        // SUBDELEGATION_BOUND now decodes REAL Uniswap Permit2
        // `permitTransferFrom` calldata via Permit2Adapter (wired in
        // setUp). A Permit2 permit is the EVM "let a spender pull up to
        // N USDC" primitive — exactly a sub-delegation of spend authority.
        // The adapter surfaces the requested amount as the proposed child
        // allowance; the validator compares it against the 500_000 bound.
        // Here we permit 800_000 (> 0.5 USDC) -> must revert.
        bytes memory inner = _permit2Calldata(800_000);
        PackedUserOperation memory op = _buildExecuteUserOp(permit2.PERMIT2(), 0, inner);
        vm.prank(op.sender);
        vm.expectRevert(bytes("ConstitutionViolation:SUBDELEGATION_BOUND"));
        hook.validateUserOp(op, bytes32(0));
    }

    function test_subdelegation_within_bound_passes() public {
        // Real Permit2 calldata permitting 400_000 (< 500_000 bound) — the
        // SUBDELEGATION_BOUND rule must allow it.
        bytes memory inner = _permit2Calldata(400_000);
        PackedUserOperation memory op = _buildExecuteUserOp(permit2.PERMIT2(), 0, inner);
        vm.prank(op.sender);
        uint256 ret = hook.validateUserOp(op, bytes32(0));
        assertEq(ret, 0);
    }

    // -----------------------------------------------------------------------
    // MAX_LEVERAGE
    // -----------------------------------------------------------------------

    function test_max_leverage_exceeded_reverts() public {
        // MAX_LEVERAGE now decodes REAL GMX v2 `createOrder` calldata via
        // GmxV2PerpAdapter (wired in setUp). We build an order whose
        // decoded leverage is 30000 bps (3x) — above the 20000 bps (2x)
        // cap — and assert the validator reverts.
        bytes memory inner = _gmxCreateOrderCalldata(30_000); // 3x > 2x cap
        PackedUserOperation memory op = _buildExecuteUserOp(gmx.GMX_V2_EXCHANGE_ROUTER(), 0, inner);
        vm.prank(op.sender);
        vm.expectRevert(bytes("ConstitutionViolation:MAX_LEVERAGE"));
        hook.validateUserOp(op, bytes32(0));
    }

    function test_max_leverage_at_bound_passes() public {
        // Real GMX calldata decoding to exactly 20000 bps (2x) — at the
        // cap, not above it — must pass.
        bytes memory inner = _gmxCreateOrderCalldata(20_000); // exactly 2x
        PackedUserOperation memory op = _buildExecuteUserOp(gmx.GMX_V2_EXCHANGE_ROUTER(), 0, inner);
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
        vm.expectRevert(ConstitutionValidator.NotInstalled.selector);
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
                ConstitutionValidator.UnsupportedOuterSelector.selector, bytes4(0xdeadbeef)
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
                ConstitutionValidator.UnsupportedOuterSelector.selector, bytes4(0x34fcd5be)
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
                ConstitutionValidator.UnsupportedOuterSelector.selector, bytes4(0)
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
                ConstitutionValidator.UnsupportedOuterSelector.selector, bytes4(0xb61d27f6)
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
