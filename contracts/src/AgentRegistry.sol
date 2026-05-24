// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

/// @notice Minimal subset of the ERC-721 surface we need: just the owner
///         lookup. We type the ERC-8004 identity registry as this interface so
///         any ERC-8004-compatible identity registry (ERC-721 by spec) works.
interface IERC721 {
    function ownerOf(uint256 tokenId) external view returns (address);
}

/// @notice Minimal BondVault surface — we only need to confirm the caller has
///         a non-zero posted bond. The full BondVault (Erasure double-burn +
///         Olas liveness) exposes `balanceOf(address)` returning the agent's
///         current bond balance.
interface IBondVault {
    function balanceOf(address account) external view returns (uint256);
}

/// @title  AgentRegistry
/// @notice The on-chain backbone of the Agent Arena. Every agent that wants to
///         participate in the arena registers here, binding:
///           - an ERC-8004 identity (NFT the caller must own),
///           - a constitution hash (the rules it commits to),
///           - a BondVault address holding its reputation stake,
///           - a dark-pool URL (its M2M endpoint for paid queries / advice).
///
///         `AgentRegistry` is the source of truth + the live event stream that
///         every downstream sub-project reads: the registry API lists agents
///         from the `register` events / views, and the operator UI decodes the
///         `AgentAction` feed to render the living economy in real time.
///
/// @dev    register requires BOTH identity ownership (anti-impersonation) and a
///         posted bond (skin in the game) — an agent with no stake cannot act
///         in the arena. One registration per identity (one agent per soul).
contract AgentRegistry {
    /// @notice A registered arena agent.
    /// @param identityId       the ERC-8004 identity NFT id the agent is bound to.
    /// @param constitutionHash hash of the rules the agent commits to follow.
    /// @param bondVault        the BondVault holding this agent's reputation stake.
    /// @param darkPoolUrl       the agent's M2M endpoint (paid queries / advice).
    /// @param operator          the address that registered + controls the agent
    ///                          (the identity owner at registration time).
    /// @param registeredAt      block timestamp at registration.
    /// @param active            false once the operator deactivates the agent.
    struct Agent {
        uint256 identityId;
        bytes32 constitutionHash;
        address bondVault;
        string darkPoolUrl;
        address operator;
        uint64 registeredAt;
        bool active;
    }

    /// @notice The ERC-8004 identity registry (ERC-721). On Arc testnet:
    ///         0x8004A818BFB912233c491871b3d84c89A494BD9e.
    IERC721 public immutable erc8004;

    /// @notice agentId => Agent. agentId is 1-indexed (0 == "no agent").
    mapping(uint256 => Agent) private _agents;

    /// @notice identityId => agentId (0 if the identity is not registered).
    mapping(uint256 => uint256) public agentByIdentity;

    /// @notice Auto-incrementing agent counter. The last assigned agentId.
    uint256 private _agentCount;

    /// @notice Emitted when a new agent registers.
    event AgentRegistered(
        uint256 indexed agentId,
        uint256 indexed identityId,
        address indexed operator,
        bytes32 constitutionHash
    );

    /// @notice The live-feed source. `kind` enumerates the arena action taken:
    ///           0 = ADVICE_PUBLISHED   (agent committed reasoning alpha)
    ///           1 = QUERY_PAID         (an x402-paid query was served)
    ///           2 = CONSTITUTION_REVERT(a user-op was reverted by the constitution)
    ///           3 = BOND_SLASHED       (the agent's bond was slashed)
    ///           4 = BOND_RELEASED      (the agent's bond was released)
    ///         `payload` is opaque, action-specific bytes (e.g. an advice hash,
    ///         a query id, a slash amount) decoded off-chain by the consumer.
    event AgentAction(
        uint256 indexed agentId,
        uint8 indexed kind,
        bytes payload,
        uint256 timestamp
    );

    /// @notice Emitted when an operator deactivates their agent.
    event AgentDeactivated(uint256 indexed agentId);

    error IdentityAlreadyRegistered();
    error NotIdentityOwner();
    error NoBond();
    error NotAgentOperator();
    error AgentDoesNotExist();

    /// @param _erc8004 the ERC-8004 identity registry (ERC-721).
    constructor(address _erc8004) {
        erc8004 = IERC721(_erc8004);
    }

    /// @notice Register an arena agent.
    /// @dev Requires the caller to (1) own the ERC-8004 identity and (2) have a
    ///      non-zero bond posted in `bondVault`. One agent per identity.
    /// @param identityId       the ERC-8004 identity NFT the caller owns.
    /// @param constitutionHash hash of the rules the agent commits to.
    /// @param bondVault        BondVault holding the caller's reputation stake.
    /// @param darkPoolUrl      the agent's M2M endpoint.
    /// @return agentId         the freshly-minted 1-indexed agent id.
    function register(
        uint256 identityId,
        bytes32 constitutionHash,
        address bondVault,
        string calldata darkPoolUrl
    ) external returns (uint256 agentId) {
        // (1) Caller must own the identity NFT (anti-impersonation).
        if (erc8004.ownerOf(identityId) != msg.sender) revert NotIdentityOwner();

        // (2) Caller must have skin in the game — a posted bond.
        if (IBondVault(bondVault).balanceOf(msg.sender) == 0) revert NoBond();

        // (3) One agent per identity.
        if (agentByIdentity[identityId] != 0) revert IdentityAlreadyRegistered();

        agentId = ++_agentCount;
        _agents[agentId] = Agent({
            identityId: identityId,
            constitutionHash: constitutionHash,
            bondVault: bondVault,
            darkPoolUrl: darkPoolUrl,
            operator: msg.sender,
            registeredAt: uint64(block.timestamp),
            active: true
        });
        agentByIdentity[identityId] = agentId;

        emit AgentRegistered(agentId, identityId, msg.sender, constitutionHash);
    }

    /// @notice Record an arena action for the live feed. Only the agent's
    ///         operator may call. See the `AgentAction` event for the `kind`
    ///         enum.
    /// @param agentId the agent taking the action.
    /// @param kind    action kind (0..4, see AgentAction).
    /// @param payload opaque, action-specific bytes decoded off-chain.
    function recordAction(
        uint256 agentId,
        uint8 kind,
        bytes calldata payload
    ) external {
        if (_agents[agentId].operator != msg.sender) revert NotAgentOperator();
        emit AgentAction(agentId, kind, payload, block.timestamp);
    }

    /// @notice Deactivate an agent. Only the operator may call. Idempotent —
    ///         re-deactivating an already-inactive agent simply re-emits.
    function deactivate(uint256 agentId) external {
        if (_agents[agentId].operator != msg.sender) revert NotAgentOperator();
        _agents[agentId].active = false;
        emit AgentDeactivated(agentId);
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    /// @notice Full Agent struct for `agentId`. Reverts if it does not exist.
    function getAgent(uint256 agentId) external view returns (Agent memory) {
        Agent memory a = _agents[agentId];
        if (a.operator == address(0)) revert AgentDoesNotExist();
        return a;
    }

    /// @notice Number of agents ever registered (the last assigned agentId).
    function agentCount() external view returns (uint256) {
        return _agentCount;
    }
}
