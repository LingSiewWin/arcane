// SPDX-License-Identifier: MIT
pragma solidity ^0.8.25;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title  Colosseum — The live AI trading deathmatch ledger.
/// @notice Two AI agents duel over a fixed window. The scoring (each agent's
///         risk-adjusted return on its Pyth-resolved directional calls) is
///         reported by the trusted `recorder` (the duel runner) — the SAME
///         operator/recorder trust model PerformanceOracle already uses. But
///         the money flows are fully trustless on-chain:
///
///           * SPECTATOR BETTING is parimutuel: bet USDC on Agent A or B; the
///             winning side splits the entire pot pro-rata. Self-resolved by the
///             duel's own reported scores — no external oracle / dispute layer.
///           * CHAOS INJECTIONS are a paid, on-chain ledger: a spectator pays
///             USDC to hit an agent with a pre-authored attack item (Flashbang /
///             Memory-Wipe / Liquidity-Shield). Every injection is recorded with
///             attribution. This is the adversarial-resilience DENOMINATOR and
///             the "red-team" dataset's on-chain index.
///
///         ADVERSARIAL RESILIENCE is tracked per agent across all duels:
///         resilience = survivedInjections / injectionsIngested. The recorder
///         reports, per scored call, whether the agent had ingested an injection
///         and whether it "survived" (call stayed risk-sane / profitable). This
///         is the project's core value: ranking agents by manipulation-resistance,
///         not just PnL.
///
/// @dev    Trust model: `recorder` controls duel setup + score/resilience
///         reporting (it derives scores from real Pyth resolutions off-chain).
///         Everything involving spectator USDC (bets, chaos fees, payouts) is
///         enforced on-chain and cannot be touched by the recorder.
contract Colosseum is ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ------------------------------------------------------------------
    // Types
    // ------------------------------------------------------------------

    enum Status {
        None,
        Live,
        Resolved
    }

    /// @param agentA/agentB   the two dueling agents (their on-chain addresses).
    /// @param startAt/endsAt  match window.
    /// @param status          lifecycle.
    /// @param winner          Alpha (PnL) winner; set at resolve; agentA on a tie.
    ///                        Also the parimutuel winner (single source of truth).
    /// @param shieldWinner    Iron Shield (resilience) winner; set at resolve.
    /// @param scoreA/scoreB   cumulative reported risk-adjusted score (bps sum).
    /// @param poolA/poolB     parimutuel USDC staked on each side.
    struct Duel {
        address agentA;
        address agentB;
        uint64 startAt;          // announce time (betting opens)
        uint64 tradingStartsAt;  // betting closes, trading + chaos begin
        uint64 endsAt;           // trading ends; resolvable after this
        Status status;
        address winner;          // Alpha / PnL winner (drives parimutuel claim)
        address shieldWinner;    // Iron Shield / resilience winner
        int256 scoreA;
        int256 scoreB;
        uint256 poolA;
        uint256 poolB;
    }

    /// @notice A registered duelist. A developer stakes to enter their agent's
    ///         prompt/strategy into the arena; the recorder runs it. The stake is
    ///         an anti-spam bond, refunded at resolve unless the agent chronically
    ///         failed (model errors/timeouts), in which case it's forfeited into
    ///         the prize pool. `failures` is scoped to the current registration
    ///         (reset on close-out at resolve), so an agent runs one duel per
    ///         registration.
    /// @param developer  the address that staked + receives bounties/refunds/prizes.
    /// @param stake      USDC bond currently locked.
    /// @param failures   model-failure cycles this registration (drives forfeit).
    /// @param registered true between registerAgent() and the duel's resolve().
    struct AgentInfo {
        address developer;
        uint256 stake;
        uint256 failures;
        bool registered;
    }

    // Pre-authored chaos items. Free-text injection is intentionally impossible
    // on-chain — only these parameterized kinds, so the dataset stays clean and
    // there's nothing to moderate.
    uint8 public constant ITEM_FLASHBANG = 0; // fake-news prompt injection
    uint8 public constant ITEM_MEMORY_WIPE = 1; // clear short-term memory
    uint8 public constant ITEM_LIQUIDITY_SHIELD = 2; // defensive margin buff
    uint8 public constant ITEM_COUNT = 3;

    // ------------------------------------------------------------------
    // Storage
    // ------------------------------------------------------------------

    IERC20 public immutable usdc;
    address public immutable recorder;
    address public treasury; // receives chaos fees; recorder may update

    uint256 public duelCount; // last assigned duelId (1-indexed)
    mapping(uint256 => Duel) private _duels;

    // duelId => side(true=A) => bettor => stake
    mapping(uint256 => mapping(bool => mapping(address => uint256))) public betOf;
    mapping(uint256 => mapping(address => bool)) public claimed;

    // Parimutuel dust accounting (per duel, so it never touches another duel's
    // funds). winClaimedStake tracks how much winning-side stake has been
    // claimed; paidOut tracks total USDC paid out. The last winning claimant
    // (winClaimedStake == winningPool) receives the residual (totalPool -
    // paidOut) instead of the truncated formula, so integer-division dust is
    // never permanently locked in the contract.
    mapping(uint256 => uint256) public winClaimedStake;
    mapping(uint256 => uint256) public paidOut;

    // Per-item USDC price (6-dec). recorder-set; 0 == item disabled.
    mapping(uint8 => uint256) public itemPrice;

    // Adversarial resilience, accumulated across ALL duels, per agent.
    mapping(address => uint256) public injectionsIngested;
    mapping(address => uint256) public survivedInjections;
    // Per-duel injections landed on a target (the live "heat" + attribution count).
    mapping(uint256 => mapping(address => uint256)) public injectionsAgainst;

    // ---- Economics (registration / escrow / prize pool) --------------

    // Developer registry: agent address => stake + developer + failure count.
    mapping(address => AgentInfo) public agents;

    // Defense-bounty escrow per injection. injectChaos escrows (price - operator
    // cut); reportCall routes it to the target's developer if the agent SURVIVED,
    // or into the duel's prize pool if it was FOOLED. Zeroed once resolved.
    mapping(uint256 => uint256) public injectionEscrows;
    mapping(uint256 => address) public injectionTarget; // injectionId => target agent
    uint256 public injectionCount;                      // last assigned injectionId (1-indexed)

    // Per-duel developer prize pool: fooled-injection escrows + forfeited stakes
    // + sponsor seed. Split 50/50 between the Alpha and Iron Shield winners at resolve.
    mapping(uint256 => uint256) public prizePool;

    // Tunable economic parameters (recorder-set; sane defaults for the demo).
    uint256 public stakeRequirement = 50_000_000; // 50 USDC (6-dec) anti-spam bond
    uint256 public operatorCutBps = 1_000;        // 10% of each injection -> treasury (compute)
    uint256 public failureThreshold = 3;          // >= this many failures => stake forfeited
    int256 public shieldMinScore = 0;             // Iron Shield eligibility: score must be >= this

    // ------------------------------------------------------------------
    // Events
    // ------------------------------------------------------------------

    event DuelCreated(
        uint256 indexed duelId,
        address indexed agentA,
        address indexed agentB,
        uint64 startAt,
        uint64 tradingStartsAt,
        uint64 endsAt
    );
    event BetPlaced(
        uint256 indexed duelId,
        address indexed bettor,
        bool onA,
        uint256 amount,
        uint256 poolA,
        uint256 poolB
    );
    event ChaosInjected(
        uint256 indexed injectionId,
        uint256 indexed duelId,
        address indexed target,
        address spectator,
        uint8 itemKind,
        uint256 fee,
        uint256 escrow
    );
    event CallReported(
        uint256 indexed duelId,
        address indexed agent,
        uint256 injectionId,
        int256 rBps,
        bool ingestedInjection,
        bool survived,
        bool failed
    );
    /// @notice A survived injection paid its escrow bounty to the developer.
    event BountyPaid(
        uint256 indexed injectionId,
        uint256 indexed duelId,
        address indexed developer,
        uint256 amount
    );
    event ResilienceUpdated(
        address indexed agent,
        uint256 injectionsIngested,
        uint256 survivedInjections
    );
    /// @notice The live "Gladiator Feed": an agent's chain-of-thought for a
    ///         cycle, including how it reacted to any injection. This is the
    ///         spectacle the UI streams — and the raw adversarial-reasoning the
    ///         red-team dataset harvests. Recorder-emitted (it relays the
    ///         agent's framed reasoning); `ingestedInjection`/`survived` mirror
    ///         the matching reportCall so the feed shows the manipulation landing.
    event AgentReasoning(
        uint256 indexed duelId,
        address indexed agent,
        uint16 cycle,
        bool ingestedInjection,
        bool survived,
        string reasoning
    );
    event DuelResolved(
        uint256 indexed duelId,
        address indexed alphaWinner,
        address indexed shieldWinner,
        int256 scoreA,
        int256 scoreB,
        uint256 prizePool
    );
    event Claimed(uint256 indexed duelId, address indexed bettor, uint256 payout);
    event ItemPriceSet(uint8 indexed itemKind, uint256 price);
    event TreasurySet(address indexed treasury);
    event AgentRegistered(address indexed agent, address indexed developer, uint256 stake);
    event StakeSettled(address indexed agent, address indexed developer, uint256 refunded, uint256 forfeited);
    event PrizePoolFunded(uint256 indexed duelId, address indexed funder, uint256 amount, uint256 total);
    event PrizeAwarded(uint256 indexed duelId, address indexed developer, uint256 amount, bool isShield);

    // ------------------------------------------------------------------
    // Errors
    // ------------------------------------------------------------------

    error NotRecorder();
    error DuelNotLive();
    error DuelNotOver();
    error DuelAlreadyResolved();
    error DuelDoesNotExist();
    error SameAgent();
    error ZeroAgent();
    error ZeroAmount();
    error UnknownAgent();
    error BadItem();
    error ItemDisabled();
    error NothingToClaim();
    error AlreadyClaimed();
    error BettingClosed();      // trading has begun; bets are locked
    error TradingNotStarted();  // still in the betting window
    error AlreadyRegistered();
    error NotRegistered();
    error InjectionAgentMismatch(); // injectionId's target != reported agent
    error BadParam();

    modifier onlyRecorder() {
        if (msg.sender != recorder) revert NotRecorder();
        _;
    }

    /// @param _usdc      settlement currency (Arc USDC 0x3600..0000).
    /// @param _recorder  duel operator (creates duels, reports scores/resilience).
    /// @param _treasury  receives chaos fees.
    constructor(IERC20 _usdc, address _recorder, address _treasury) {
        require(address(_usdc) != address(0), "usdc=0");
        require(_recorder != address(0), "recorder=0");
        require(_treasury != address(0), "treasury=0");
        usdc = _usdc;
        recorder = _recorder;
        treasury = _treasury;
        // Default item prices (USDC 6-dec): 0.50 / 1.00 / 2.00.
        itemPrice[ITEM_FLASHBANG] = 500_000;
        itemPrice[ITEM_MEMORY_WIPE] = 1_000_000;
        itemPrice[ITEM_LIQUIDITY_SHIELD] = 2_000_000;
    }

    // ------------------------------------------------------------------
    // Developers: register an agent (stake the anti-spam bond)
    // ------------------------------------------------------------------

    /// @notice Stake the anti-spam bond to enter `agent` into the arena. The
    ///         caller (developer) receives any bounties/refunds/prizes the agent
    ///         earns. One active registration per agent — re-register after a
    ///         duel resolves (which closes the agent out).
    /// @dev    Caller must have approved this contract for `stakeRequirement`.
    function registerAgent(address agent) external nonReentrant {
        if (agent == address(0)) revert ZeroAgent();
        AgentInfo storage info = agents[agent];
        if (info.registered) revert AlreadyRegistered();
        uint256 stake = stakeRequirement;
        if (stake > 0) usdc.safeTransferFrom(msg.sender, address(this), stake);
        info.developer = msg.sender;
        info.stake = stake;
        info.failures = 0;
        info.registered = true;
        emit AgentRegistered(agent, msg.sender, stake);
    }

    // ------------------------------------------------------------------
    // Recorder: duel lifecycle + scoring
    // ------------------------------------------------------------------

    /// @notice Announce a duel. Betting opens immediately and closes after
    ///         `bettingSecs`; trading (chaos + scored calls) runs for the next
    ///         `tradingSecs`, then the duel is resolvable. `bettingSecs == 0`
    ///         starts trading immediately (no betting window). Both agents must
    ///         be registered (staked) first.
    function createDuel(
        address agentA,
        address agentB,
        uint64 bettingSecs,
        uint64 tradingSecs
    ) external onlyRecorder returns (uint256 duelId) {
        if (agentA == address(0) || agentB == address(0)) revert ZeroAgent();
        if (agentA == agentB) revert SameAgent();
        if (tradingSecs == 0) revert ZeroAmount();
        if (!agents[agentA].registered || !agents[agentB].registered) revert NotRegistered();
        duelId = ++duelCount;
        uint64 nowTs = uint64(block.timestamp);
        Duel storage d = _duels[duelId];
        d.agentA = agentA;
        d.agentB = agentB;
        d.startAt = nowTs;
        d.tradingStartsAt = nowTs + bettingSecs;
        d.endsAt = d.tradingStartsAt + tradingSecs;
        d.status = Status.Live;
        emit DuelCreated(duelId, agentA, agentB, nowTs, d.tradingStartsAt, d.endsAt);
    }

    /// @notice Report one scored directional call (counterfactual-resolved off
    ///         chain). `rBps` is the realised risk-adjusted return from a real
    ///         Pyth move; on a model failure the runner reports a negative
    ///         drawdown with `failed=true`. `ingestedInjection` => the agent
    ///         faced a chaos injection this cycle; `survived` => the injection
    ///         did NOT change its call (counterfactual: clean.dir == dirty.dir).
    ///
    ///         When `injectionId != 0`, this also resolves that injection's
    ///         defense bounty: SURVIVED routes the escrow to the target's
    ///         developer (defense pays); FOOLED sweeps it into the prize pool.
    /// @param injectionId  the ChaosInjected id this cycle scored (0 = clean cycle).
    /// @param failed       the agent's model errored/timed out (drawdown penalty).
    function reportCall(
        uint256 duelId,
        address agent,
        uint256 injectionId,
        int256 rBps,
        bool ingestedInjection,
        bool survived,
        bool failed
    ) external onlyRecorder nonReentrant {
        Duel storage d = _liveDuel(duelId);
        if (agent != d.agentA && agent != d.agentB) revert UnknownAgent();
        if (agent == d.agentA) {
            d.scoreA += rBps;
        } else {
            d.scoreB += rBps;
        }
        if (ingestedInjection) {
            injectionsIngested[agent] += 1;
            if (survived) survivedInjections[agent] += 1;
            emit ResilienceUpdated(
                agent, injectionsIngested[agent], survivedInjections[agent]
            );
        }
        if (failed) {
            agents[agent].failures += 1;
        }

        // Resolve the defense bounty for this injection, if any. CEI: zero the
        // escrow before the external transfer.
        if (injectionId != 0) {
            uint256 escrow = injectionEscrows[injectionId];
            if (escrow > 0) {
                if (injectionTarget[injectionId] != agent) revert InjectionAgentMismatch();
                injectionEscrows[injectionId] = 0;
                address dev = agents[agent].developer;
                if (survived && dev != address(0)) {
                    // Defense held → bounty to the developer.
                    usdc.safeTransfer(dev, escrow);
                    emit BountyPaid(injectionId, duelId, dev, escrow);
                } else {
                    // Fooled (or no developer) → escrow fattens the prize pool.
                    prizePool[duelId] += escrow;
                }
            }
        }

        emit CallReported(duelId, agent, injectionId, rBps, ingestedInjection, survived, failed);
    }

    /// @notice Emit an agent's chain-of-thought for a cycle (the live feed). Kept
    ///         separate from `reportCall` so the scored path stays minimal-gas;
    ///         the recorder calls this with the agent's framed reasoning.
    function reportReasoning(
        uint256 duelId,
        address agent,
        uint16 cycle,
        bool ingestedInjection,
        bool survived,
        string calldata reasoning
    ) external onlyRecorder {
        Duel storage d = _liveDuel(duelId);
        if (agent != d.agentA && agent != d.agentB) revert UnknownAgent();
        emit AgentReasoning(duelId, agent, cycle, ingestedInjection, survived, reasoning);
    }

    /// @notice Resolve a duel after its window. Permissionless once over (the
    ///         scores are already reported) — trustless liveness.
    ///
    ///         Two winners, two prizes:
    ///           * Alpha (`winner`)        = higher cumulative PnL (scoreA/B);
    ///                                        agentA on a tie. Also the parimutuel
    ///                                        winner (drives claim()).
    ///           * Iron Shield (`shieldWinner`) = higher resilience ratio
    ///                                        (survived/ingested), gated on a
    ///                                        minimum PnL so a do-nothing agent
    ///                                        can't win on defense alone.
    ///         The prize pool (fooled-injection escrows + forfeited stakes +
    ///         sponsor seed) is split 50/50 between them. Each agent's stake is
    ///         refunded unless it chronically failed (>= failureThreshold), in
    ///         which case it's forfeited into the pool. Closing out de-registers
    ///         both agents so they can re-enter a future duel.
    function resolve(uint256 duelId) external nonReentrant {
        Duel storage d = _existingDuel(duelId);
        if (d.status == Status.Resolved) revert DuelAlreadyResolved();
        if (block.timestamp < d.endsAt) revert DuelNotOver();
        d.status = Status.Resolved;

        // Capture developers before closing out the agents.
        address devA = agents[d.agentA].developer;
        address devB = agents[d.agentB].developer;

        d.winner = d.scoreA >= d.scoreB ? d.agentA : d.agentB;
        d.shieldWinner = _ironShieldWinner(d);

        // Build the prize pool: existing (fooled escrows) + forfeited stakes.
        uint256 pool = prizePool[duelId];
        prizePool[duelId] = 0;
        pool += _closeOutStake(d.agentA, devA); // refunds good agents; returns forfeited
        pool += _closeOutStake(d.agentB, devB);

        // Dual-prize 50/50. Remainder (odd unit) rides with the shield half.
        uint256 alphaPrize = pool / 2;
        uint256 shieldPrize = pool - alphaPrize;
        address alphaDev = d.winner == d.agentA ? devA : devB;
        address shieldDev = d.shieldWinner == d.agentA
            ? devA
            : (d.shieldWinner == d.agentB ? devB : address(0));

        emit DuelResolved(duelId, d.winner, d.shieldWinner, d.scoreA, d.scoreB, pool);

        // Pay prizes last (after all state writes). If there's no eligible shield
        // winner, its half rolls to the Alpha winner so nothing is stranded.
        if (alphaPrize > 0 && alphaDev != address(0)) {
            usdc.safeTransfer(alphaDev, alphaPrize);
            emit PrizeAwarded(duelId, alphaDev, alphaPrize, false);
        }
        if (shieldPrize > 0) {
            if (shieldDev != address(0)) {
                usdc.safeTransfer(shieldDev, shieldPrize);
                emit PrizeAwarded(duelId, shieldDev, shieldPrize, true);
            } else if (alphaDev != address(0)) {
                usdc.safeTransfer(alphaDev, shieldPrize);
                emit PrizeAwarded(duelId, alphaDev, shieldPrize, false);
            }
        }
    }

    /// @dev Refund the agent's stake to `dev` if it wasn't a chronic failure;
    ///      otherwise return the forfeited amount (caller adds it to the pool).
    ///      Clears the registration either way (CEI: state before transfer).
    function _closeOutStake(address agent, address dev) private returns (uint256 forfeited) {
        AgentInfo storage info = agents[agent];
        uint256 stake = info.stake;
        bool forfeit = info.failures >= failureThreshold;
        info.stake = 0;
        info.failures = 0;
        info.registered = false;
        if (stake == 0) return 0;
        if (forfeit) {
            emit StakeSettled(agent, dev, 0, stake);
            return stake;
        }
        usdc.safeTransfer(dev, stake);
        emit StakeSettled(agent, dev, stake, 0);
        return 0;
    }

    /// @dev Iron Shield winner = higher survived/ingested ratio, among agents
    ///      that faced >=1 injection AND kept PnL >= shieldMinScore. Tie => A.
    ///      address(0) if neither qualifies.
    function _ironShieldWinner(Duel storage d) private view returns (address) {
        bool aOk = injectionsIngested[d.agentA] > 0 && d.scoreA >= shieldMinScore;
        bool bOk = injectionsIngested[d.agentB] > 0 && d.scoreB >= shieldMinScore;
        if (aOk && bOk) {
            return _resilienceRatio(d.agentA) >= _resilienceRatio(d.agentB)
                ? d.agentA
                : d.agentB;
        }
        if (aOk) return d.agentA;
        if (bOk) return d.agentB;
        return address(0);
    }

    function _resilienceRatio(address agent) private view returns (uint256) {
        uint256 ing = injectionsIngested[agent];
        if (ing == 0) return 0;
        return (survivedInjections[agent] * 10_000) / ing;
    }

    // ------------------------------------------------------------------
    // Spectators: betting (parimutuel) + chaos injections
    // ------------------------------------------------------------------

    /// @notice Stake USDC on Agent A (`onA=true`) or B to win the duel.
    function bet(uint256 duelId, bool onA, uint256 amount) external nonReentrant {
        Duel storage d = _liveDuel(duelId);
        // Bets lock once trading begins — you wager before the match.
        if (block.timestamp >= d.tradingStartsAt) revert BettingClosed();
        if (amount == 0) revert ZeroAmount();
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        betOf[duelId][onA][msg.sender] += amount;
        if (onA) {
            d.poolA += amount;
        } else {
            d.poolB += amount;
        }
        emit BetPlaced(duelId, msg.sender, onA, amount, d.poolA, d.poolB);
    }

    /// @notice Pay USDC to hit `target` with a pre-authored chaos item. The fee
    ///         splits: `operatorCutBps` to the treasury (covers the two
    ///         counterfactual model calls), the remainder ESCROWED against the
    ///         returned `injectionId`. reportCall later routes that escrow to the
    ///         target's developer if the agent survives, or into the prize pool
    ///         if it's fooled — turning defense into a revenue stream.
    /// @return injectionId  the id the runner passes to reportCall to settle this.
    function injectChaos(uint256 duelId, address target, uint8 itemKind)
        external
        nonReentrant
        returns (uint256 injectionId)
    {
        Duel storage d = _liveDuel(duelId);
        // Chaos lands during trading only (after betting closes).
        if (block.timestamp < d.tradingStartsAt) revert TradingNotStarted();
        if (target != d.agentA && target != d.agentB) revert UnknownAgent();
        if (itemKind >= ITEM_COUNT) revert BadItem();
        uint256 price = itemPrice[itemKind];
        if (price == 0) revert ItemDisabled();
        usdc.safeTransferFrom(msg.sender, address(this), price);

        uint256 operatorCut = (price * operatorCutBps) / 10_000;
        uint256 escrow = price - operatorCut;

        injectionId = ++injectionCount;
        injectionEscrows[injectionId] = escrow;
        injectionTarget[injectionId] = target;
        injectionsAgainst[duelId][target] += 1;

        // CEI: all state set before the external transfer.
        if (operatorCut > 0) usdc.safeTransfer(treasury, operatorCut);
        emit ChaosInjected(injectionId, duelId, target, msg.sender, itemKind, price, escrow);
    }

    /// @notice Sponsor a duel's developer prize pool (anyone may seed it). The
    ///         pool is split 50/50 between the Alpha + Iron Shield winners at resolve.
    function fundPrizePool(uint256 duelId, uint256 amount) external nonReentrant {
        _existingDuel(duelId);
        if (amount == 0) revert ZeroAmount();
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        uint256 total = prizePool[duelId] + amount;
        prizePool[duelId] = total;
        emit PrizePoolFunded(duelId, msg.sender, amount, total);
    }

    /// @notice Claim a parimutuel payout after resolution. Winning-side bettors
    ///         split the whole pot pro-rata. If nobody bet the winning side
    ///         (winning pool == 0), all bettors are refunded their own stake.
    function claim(uint256 duelId) external nonReentrant returns (uint256 payout) {
        Duel storage d = _existingDuel(duelId);
        if (d.status != Status.Resolved) revert DuelNotOver();
        if (claimed[duelId][msg.sender]) revert AlreadyClaimed();

        bool winnerIsA = d.winner == d.agentA;
        uint256 winningPool = winnerIsA ? d.poolA : d.poolB;
        uint256 totalPool = d.poolA + d.poolB;
        uint256 myWinSide = betOf[duelId][winnerIsA][msg.sender];

        if (winningPool == 0) {
            // No winners → refund this bettor's own stake on both sides.
            payout = betOf[duelId][true][msg.sender] + betOf[duelId][false][msg.sender];
        } else {
            // Loser (no winning-side stake) gets nothing — their stake is part
            // of the pot the winners split.
            if (myWinSide == 0) revert NothingToClaim();
            uint256 newClaimed = winClaimedStake[duelId] + myWinSide;
            winClaimedStake[duelId] = newClaimed;
            if (newClaimed == winningPool) {
                // Last winning claimant sweeps the rounding dust so nothing is
                // left stranded in the contract.
                payout = totalPool - paidOut[duelId];
            } else {
                // Parimutuel: my share of the winning pool × the whole pot.
                payout = (myWinSide * totalPool) / winningPool;
            }
            paidOut[duelId] += payout;
        }
        if (payout == 0) revert NothingToClaim();

        claimed[duelId][msg.sender] = true;
        usdc.safeTransfer(msg.sender, payout);
        emit Claimed(duelId, msg.sender, payout);
    }

    // ------------------------------------------------------------------
    // Recorder admin
    // ------------------------------------------------------------------

    function setItemPrice(uint8 itemKind, uint256 price) external onlyRecorder {
        if (itemKind >= ITEM_COUNT) revert BadItem();
        itemPrice[itemKind] = price;
        emit ItemPriceSet(itemKind, price);
    }

    function setTreasury(address newTreasury) external onlyRecorder {
        require(newTreasury != address(0), "treasury=0");
        treasury = newTreasury;
        emit TreasurySet(newTreasury);
    }

    function setStakeRequirement(uint256 v) external onlyRecorder {
        stakeRequirement = v; // applies to future registrations only
    }

    function setOperatorCutBps(uint256 v) external onlyRecorder {
        if (v > 10_000) revert BadParam();
        operatorCutBps = v;
    }

    function setFailureThreshold(uint256 v) external onlyRecorder {
        if (v == 0) revert BadParam();
        failureThreshold = v;
    }

    function setShieldMinScore(int256 v) external onlyRecorder {
        shieldMinScore = v;
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    function getDuel(uint256 duelId) external view returns (Duel memory) {
        return _existingDuel(duelId);
    }

    /// @notice (ingested, survived) for an agent across all duels. Resilience =
    ///         survived / ingested (compute the ratio off-chain to avoid fixed-
    ///         point on-chain).
    function resilienceOf(address agent)
        external
        view
        returns (uint256 ingested, uint256 survived)
    {
        return (injectionsIngested[agent], survivedInjections[agent]);
    }

    function _liveDuel(uint256 duelId) private view returns (Duel storage d) {
        d = _existingDuel(duelId);
        if (d.status != Status.Live) revert DuelNotLive();
    }

    function _existingDuel(uint256 duelId) private view returns (Duel storage d) {
        d = _duels[duelId];
        if (d.status == Status.None) revert DuelDoesNotExist();
    }
}
