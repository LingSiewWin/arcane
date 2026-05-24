// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Script, console2} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ConstitutionRegistry} from "../src/ConstitutionRegistry.sol";
import {ConstitutionValidator} from "../src/ConstitutionValidator.sol";
import {ConstitutionHook} from "../src/ConstitutionHook.sol";
import {MemoryAnchor} from "../src/MemoryAnchor.sol";
import {BondVault} from "../src/BondVault.sol";
import {PerformanceOracle, IBondVault} from "../src/PerformanceOracle.sol";
import {AgentRegistry} from "../src/AgentRegistry.sol";
import {IPyth} from "@pythnetwork/pyth-sdk-solidity/IPyth.sol";

/// @title  Deploy
/// @notice Foundry deployment script for the Constrained Cognition contracts
///         on Arc testnet (chain id 5042002 == 0x4cef52).
///
/// @dev    Usage:
///           forge script script/Deploy.s.sol:Deploy \
///               --rpc-url $ARC_RPC \
///               --private-key $DEPLOYER_PK \
///               --broadcast --slow
///
///         Environment:
///           ARC_RPC          (e.g. https://rpc.testnet.arc.network)
///           DEPLOYER_PK      private key of an EOA funded with USDC for gas
///           ARC_USDC         optional override; defaults to canonical
///                            0x3600000000000000000000000000000000000000
///           BOND_ORACLE      address authorized to slash; defaults to deployer
///           BOND_INSURANCE   sink for slashed funds; defaults to deployer
///           BOND_WINDOW_SECS release window in seconds; defaults to 7 days
contract Deploy is Script {
    address internal constant ARC_TESTNET_USDC = 0x3600000000000000000000000000000000000000;
    uint256 internal constant ARC_TESTNET_CHAIN_ID = 5042002;
    // Arc testnet ERC-8004 identity registry (ERC-721). Used to bind
    // MemoryAnchor entries to an agent identity via `anchor(uint256,bytes32)`.
    address internal constant ARC_TESTNET_IDENTITY_REGISTRY =
        0x8004A818BFB912233c491871b3d84c89A494BD9e;
    // Canonical Pyth pull-oracle on Arc testnet (verified on-chain).
    address internal constant ARC_TESTNET_PYTH =
        0x2880aB155794e7179c9eE2e38200202908C17B43;

    function run() external {
        // Sanity: warn if not on Arc testnet (still proceed - useful for local dry-runs).
        if (block.chainid != ARC_TESTNET_CHAIN_ID) {
            console2.log("WARNING: chainid =", block.chainid);
            console2.log("expected Arc testnet 5042002 (0x4cef52)");
        }

        address usdc = vm.envOr("ARC_USDC", ARC_TESTNET_USDC);
        address identityRegistry = vm.envOr(
            "ARC_IDENTITY_REGISTRY", ARC_TESTNET_IDENTITY_REGISTRY
        );
        // Signer resolved in a helper so `pk` stays out of run()'s stack frame
        // (avoids stack-too-deep). Broadcasts with PRIVATE_KEY if set, else the
        // default sender (so dry-runs/sims still work without a key).
        address deployer = _beginBroadcast();
        address oracle = vm.envOr("BOND_ORACLE", deployer);
        uint256 window = vm.envOr("BOND_WINDOW_SECS", uint256(7 days));
        uint256 livenessTimeout = vm.envOr("BOND_LIVENESS_SECS", uint256(3 days));

        ConstitutionRegistry registry = new ConstitutionRegistry();
        // ConstitutionValidator is the ERC-7579 validator (type 1) — the demo's
        // gatekeeper that reverts violating user-ops. ConstitutionHook is the
        // type-4 hook with preCheck/postCheck for executor-initiated calls.
        ConstitutionValidator validator = new ConstitutionValidator(registry);
        ConstitutionHook hook = new ConstitutionHook(registry, usdc);
        MemoryAnchor anchor = new MemoryAnchor(identityRegistry);
        // BondVault: Erasure double-burn (no insurance pool) + Olas-style
        // liveness timeout. Slashed funds burn to 0x…dEaD, not to an insurer.
        BondVault vault = new BondVault(IERC20(usdc), oracle, window, livenessTimeout);

        // PerformanceOracle: the real Pyth-driven slash judge. It becomes the
        // vault's `oracle`, so it (and only it) can slash — and only after
        // posting its own Erasure counter-bond. The recorder (advice committer)
        // defaults to the deployer.
        address pythAddr = vm.envOr("ARC_PYTH", ARC_TESTNET_PYTH);
        address recorder = vm.envOr("PERF_RECORDER", deployer);
        PerformanceOracle perfOracle = new PerformanceOracle(
            IPyth(pythAddr),
            IBondVault(address(vault)),
            IERC20(usdc),
            recorder
        );
        vault.setOracle(address(perfOracle));

        // AgentRegistry: the Agent Arena backbone. Binds each agent to its
        // ERC-8004 identity (the same registry MemoryAnchor uses) and is the
        // source of truth + live `AgentAction` event stream the API + UI read.
        // Deployed + logged inside a block scope so its local is freed before
        // the trailing console.log block — keeping run() under the EVM 16-slot
        // stack limit without enabling via-ir (which would touch every contract).
        {
            AgentRegistry agentRegistry = new AgentRegistry(identityRegistry);
            _logAgentRegistry(address(agentRegistry), identityRegistry);
        }

        vm.stopBroadcast();

        console2.log("ConstitutionRegistry:", address(registry));
        console2.log("ConstitutionValidator:", address(validator));
        console2.log("ConstitutionHook    :", address(hook));
        console2.log("MemoryAnchor        :", address(anchor));
        console2.log("  identityRegistry  :", identityRegistry);
        console2.log("BondVault           :", address(vault));
        console2.log("  bondToken (USDC)  :", usdc);
        console2.log("  oracle            :", address(perfOracle));
        console2.log("  releaseWindow     :", window);
        console2.log("  livenessTimeout   :", livenessTimeout);
        console2.log("PerformanceOracle   :", address(perfOracle));
        console2.log("  pyth              :", pythAddr);
        console2.log("  recorder          :", recorder);
    }

    /// @dev Split out of `run()` to keep its stack frame under the 16-slot
    ///      limit (the AgentRegistry deploy pushed `run()` over without via-ir).
    function _logAgentRegistry(address agentRegistry, address identityRegistry) internal pure {
        console2.log("AgentRegistry       :", agentRegistry);
        console2.log("  erc8004 (identity):", identityRegistry);
    }

    /// @dev Start the broadcast and return the deployer address. If PRIVATE_KEY
    ///      is in the env (the launcher resolves the keystore in-process and
    ///      passes it as PRIVATE_KEY — never on argv), broadcast with it;
    ///      otherwise use the default sender so dry-runs/sims work keyless.
    ///      Kept as a helper so `pk` never enters run()'s stack frame.
    function _beginBroadcast() internal returns (address deployer) {
        uint256 pk = vm.envOr("PRIVATE_KEY", uint256(0));
        if (pk != 0) {
            deployer = vm.addr(pk);
            vm.startBroadcast(pk);
        } else {
            deployer = msg.sender;
            vm.startBroadcast();
        }
    }
}
