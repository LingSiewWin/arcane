// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {Script, console2} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ConstitutionRegistry} from "../src/ConstitutionRegistry.sol";
import {ConstitutionHook} from "../src/ConstitutionHook.sol";
import {MemoryAnchor} from "../src/MemoryAnchor.sol";
import {BondVault} from "../src/BondVault.sol";

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
        address deployer = msg.sender;
        address oracle = vm.envOr("BOND_ORACLE", deployer);
        address insurance = vm.envOr("BOND_INSURANCE", deployer);
        uint256 window = vm.envOr("BOND_WINDOW_SECS", uint256(7 days));

        vm.startBroadcast();

        ConstitutionRegistry registry = new ConstitutionRegistry();
        ConstitutionHook hook = new ConstitutionHook(registry);
        MemoryAnchor anchor = new MemoryAnchor(identityRegistry);
        BondVault vault = new BondVault(IERC20(usdc), oracle, insurance, window);

        vm.stopBroadcast();

        console2.log("ConstitutionRegistry:", address(registry));
        console2.log("ConstitutionHook    :", address(hook));
        console2.log("MemoryAnchor        :", address(anchor));
        console2.log("  identityRegistry  :", identityRegistry);
        console2.log("BondVault           :", address(vault));
        console2.log("  bondToken (USDC)  :", usdc);
        console2.log("  oracle            :", oracle);
        console2.log("  insurance         :", insurance);
        console2.log("  releaseWindow     :", window);
    }
}
