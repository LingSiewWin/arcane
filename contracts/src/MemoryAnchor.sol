// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

/// @notice Minimal subset of the ERC-721 surface we need: just the owner
///         lookup. We type the registry as this interface so any
///         ERC-8004-compatible identity registry (ERC-721 by spec) works.
interface IERC721Minimal {
    function ownerOf(uint256 tokenId) external view returns (address);
}

/// @title  MemoryAnchor
/// @notice Per-agent commitment of the Merkle root of pinned memory. The
///         off-chain MemoryService recomputes the root over its pinned slot
///         each cycle; equality between this root and the on-chain anchor
///         proves the agent's memory still contains the rules it was deployed
///         with.
///
///         F10 hardening: the new ``anchor(uint256 identityId, bytes32 root)``
///         entry point binds each anchor to an ERC-8004 identity. The caller
///         must own the identity NFT (verified via ``ownerOf``), so off-chain
///         observers can answer "which msg.sender is the real Alice?" by
///         consulting the identity registry — they no longer have to trust
///         a raw EOA. The legacy address-keyed entry points
///         (``anchor(bytes32)`` / ``anchorByAddress(bytes32)``) are preserved
///         for backward compatibility and emit the same unified event with
///         ``identityId = 0``.
///
/// @dev    Keep this tiny — the cost of an anchor tx should be a few k gas so
///         the agent can re-anchor on every decay step.
contract MemoryAnchor {
    struct Anchor {
        bytes32 root;
        uint64 timestamp;
        uint64 sequence;
    }

    /// @notice An immutable, append-only history record. Once written it is
    ///         never overwritten — a new anchor pushes a new record. This is
    ///         the B13 fix: ERC-8004 identity NFTs are transferable, so the
    ///         identity owner can change over time. Binding ``ownerAtAnchor``
    ///         into each record means an off-chain observer reading the
    ///         history can see *who* anchored each root, even after the
    ///         identity has been transferred to a new owner. Old anchors stay
    ///         visible with their original owner; the new owner can still
    ///         append, but cannot rewrite or hide the past.
    struct AnchorRecord {
        bytes32 root;
        address ownerAtAnchor;
        uint256 timestamp;
    }

    /// @notice ERC-8004 identity registry (ERC-721). Set at deploy time. On
    ///         Arc testnet this is 0x8004A818BFB912233c491871b3d84c89A494BD9e.
    address public immutable identityRegistry;

    // --- Legacy address-keyed state (preserved for backward compat) -----
    /// @notice agent => most recent anchor
    mapping(address => Anchor) public latest;
    /// @notice agent => total anchors written
    mapping(address => uint64) public count;

    // --- New identity-keyed state ---------------------------------------
    /// @notice identityId => most recent anchor
    mapping(uint256 => Anchor) public latestByIdentity;
    /// @notice identityId => total anchors written
    mapping(uint256 => uint64) public countByIdentity;

    // --- Append-only history (B13) --------------------------------------
    /// @notice identityId => full, immutable, ordered list of every anchor
    ///         ever made under that identity. Index == sequence (0-based).
    ///         identityId == 0 holds the merged history of all legacy
    ///         address-keyed anchors; those are UNVERIFIED (no ``ownerOf``
    ///         check) and observers must treat ``ownerAtAnchor`` there as a
    ///         self-asserted, sock-puppet-possible EOA.
    mapping(uint256 => AnchorRecord[]) private _history;

    /// @notice Unified anchor event. ``identityId == 0`` means a legacy
    ///         address-keyed anchor; nonzero ``identityId`` means the anchor
    ///         is bound to an ERC-8004 identity owned by ``agent``.
    event MemoryAnchored(
        address indexed agent,
        uint256 indexed identityId,
        bytes32 root,
        uint256 timestamp
    );

    error EmptyRoot();
    error NotIdentityOwner();
    error SequenceOutOfRange();

    constructor(address identityRegistry_) {
        identityRegistry = identityRegistry_;
    }

    // --------------------------------------------------------------------
    // New: identity-bound anchor
    // --------------------------------------------------------------------

    /// @notice Commit ``root`` as the current pinned-memory root for the
    ///         ERC-8004 identity ``identityId``. The caller must own the
    ///         identity NFT in the registry passed to the constructor.
    /// @dev    The owner check goes through the registry's ``ownerOf``. If
    ///         the token doesn't exist, the registry reverts and that
    ///         revert bubbles up.
    function anchor(uint256 identityId, bytes32 root) external {
        if (root == bytes32(0)) revert EmptyRoot();
        if (
            IERC721Minimal(identityRegistry).ownerOf(identityId) != msg.sender
        ) {
            revert NotIdentityOwner();
        }
        uint64 next = countByIdentity[identityId] + 1;
        latestByIdentity[identityId] = Anchor({
            root: root,
            timestamp: uint64(block.timestamp),
            sequence: next
        });
        countByIdentity[identityId] = next;
        // Append-only provenance record. msg.sender == the verified
        // ownerOf(identityId) at this block, so it is the true owner-at-time.
        _history[identityId].push(
            AnchorRecord({
                root: root,
                ownerAtAnchor: msg.sender,
                timestamp: block.timestamp
            })
        );
        emit MemoryAnchored(msg.sender, identityId, root, block.timestamp);
    }

    // --------------------------------------------------------------------
    // Legacy: address-keyed anchor (kept for backward compat with the
    // existing off-chain scripts that do not yet know about identity IDs)
    // --------------------------------------------------------------------

    /// @notice Legacy address-keyed anchor. Identical semantics to the
    ///         pre-F10 ``anchor(bytes32)`` — same selector, same storage —
    ///         so callers using the old ABI keep working.
    /// @dev    Emits the unified event with ``identityId = 0`` to signal
    ///         "not bound to an identity".
    function anchor(bytes32 root) public {
        if (root == bytes32(0)) revert EmptyRoot();
        uint64 next = count[msg.sender] + 1;
        latest[msg.sender] = Anchor({
            root: root,
            timestamp: uint64(block.timestamp),
            sequence: next
        });
        count[msg.sender] = next;
        // Legacy anchors share a single identityId == 0 history. These are
        // UNVERIFIED — there is no ownerOf check — so ownerAtAnchor here is a
        // self-asserted EOA (sock-puppet-possible). Recorded for auditability
        // anyway so the append-only invariant holds for every code path.
        _history[0].push(
            AnchorRecord({
                root: root,
                ownerAtAnchor: msg.sender,
                timestamp: block.timestamp
            })
        );
        emit MemoryAnchored(msg.sender, 0, root, block.timestamp);
    }

    /// @notice Spec-named alias for ``anchor(bytes32)``. Functionally
    ///         identical; lets callers reach the legacy path under either
    ///         selector.
    function anchorByAddress(bytes32 root) external {
        anchor(root);
    }

    // --------------------------------------------------------------------
    // Views
    // --------------------------------------------------------------------

    /// @notice Convenience: read the last anchored root for ``agent`` from
    ///         the legacy address-keyed slot.
    function rootOf(address agent) external view returns (bytes32) {
        return latest[agent].root;
    }

    /// @notice Convenience: read the last anchored root for an ERC-8004
    ///         identity.
    function rootOfIdentity(uint256 identityId) external view returns (bytes32) {
        return latestByIdentity[identityId].root;
    }

    // --------------------------------------------------------------------
    // Append-only history views (B13)
    // --------------------------------------------------------------------

    /// @notice Number of anchors ever recorded for ``identityId`` (the length
    ///         of its immutable history). Valid ``sequence`` values for the
    ///         views below are ``0 .. historyLength - 1``.
    function historyLength(uint256 identityId) external view returns (uint256) {
        return _history[identityId].length;
    }

    /// @notice The full provenance of the anchor at position ``sequence`` for
    ///         ``identityId``. Because the history is append-only, the record
    ///         at any past ``sequence`` is immutable: it always reflects the
    ///         root and the owner-at-the-time, even after the identity NFT has
    ///         been transferred to a new owner.
    /// @dev    Reverts ``SequenceOutOfRange`` if no such anchor exists.
    function anchorAt(uint256 identityId, uint64 sequence)
        external
        view
        returns (bytes32 root, address ownerAtAnchor, uint256 timestamp)
    {
        AnchorRecord[] storage h = _history[identityId];
        if (sequence >= h.length) revert SequenceOutOfRange();
        AnchorRecord storage rec = h[sequence];
        return (rec.root, rec.ownerAtAnchor, rec.timestamp);
    }

    /// @notice The owner who made the anchor at ``sequence`` under
    ///         ``identityId``. For identity-bound anchors this is the verified
    ///         ``ownerOf(identityId)`` as of that block — so even after a
    ///         transfer, history attributes each root to its real author.
    /// @dev    Reverts ``SequenceOutOfRange`` if no such anchor exists.
    function historicalOwnerOf(uint256 identityId, uint64 sequence)
        external
        view
        returns (address)
    {
        AnchorRecord[] storage h = _history[identityId];
        if (sequence >= h.length) revert SequenceOutOfRange();
        return h[sequence].ownerAtAnchor;
    }

    /// @notice The most recent anchored root for ``identityId`` read straight
    ///         from the append-only history (``bytes32(0)`` if none yet).
    ///         Equivalent to ``rootOfIdentity`` but sourced from history, so
    ///         observers can verify the two never diverge.
    function latestRoot(uint256 identityId) external view returns (bytes32) {
        AnchorRecord[] storage h = _history[identityId];
        uint256 n = h.length;
        if (n == 0) return bytes32(0);
        return h[n - 1].root;
    }
}
