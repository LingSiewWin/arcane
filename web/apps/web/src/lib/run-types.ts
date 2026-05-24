/**
 * Typed model of scripts/demo_output.jsonl — the live/local run artifact.
 * Each line is one step. Fields are optional/loose because the evidence shape
 * varies per step; we narrow it per step in the panels. No `any`.
 */

export interface DeployedContract {
  address: string;
  tx_hash: string;
  [k: string]: unknown;
}

export interface RunAddresses {
  ConstitutionRegistry?: DeployedContract;
  ConstitutionHook?: DeployedContract;
  ConstitutionValidator?: DeployedContract;
  IdentityRegistry?: DeployedContract & {
    minted_identity_id?: number;
    minted_to?: string;
    mint_tx?: string;
  };
  MemoryAnchor?: DeployedContract & { identity_id?: number };
  BondVault?: DeployedContract;
  PerformanceOracle?: DeployedContract & { pyth?: string; set_oracle_tx?: string };
  GmxV2PerpAdapter?: DeployedContract;
  identity_id?: number;
}

export interface RecordAdvice {
  record_advice_tx?: string;
  update_fee?: number;
  hermes_p0?: number;
  hermes_p0_float?: number;
  hermes_p0_publish_time?: number;
}

export interface BondPost {
  approve_tx?: string;
  post_tx?: string;
}

export interface StepEvidence {
  // step 1 — spawn
  deployer?: string;
  addresses?: RunAddresses;
  constitution_hash?: string;
  constitution_hash_onchain?: string;
  constitution_hash_local?: string;
  eoa?: string;
  budget_usdc?: number;
  rule_kinds?: string[];
  // step 2 — query
  alice_url?: string;
  prompt?: string;
  result_count?: number;
  top_trace_id?: string;
  n_results?: number;
  // step 3 — select
  selected?: string;
  selected_score?: number;
  selected_text?: string;
  interpretation?: string;
  // step 4 — revert
  expected?: string;
  hook_install_tx?: string;
  hook_address?: string;
  sender?: string;
  tx_hash?: string;
  receipt_status?: number;
  block_number?: number;
  revert_reason?: string;
  expected_rule?: string;
  oversize_usdc?: number;
  amount_units?: number;
  inner_selector?: string;
  execute_calldata_hex?: string;
  // step 5 — anchor
  pinned_root_before?: string;
  pinned_root_after?: string;
  entries_before?: number;
  entries_after?: number;
  evicted?: number;
  advance_seconds?: number;
  root?: string;
  anchor?: string;
  identity_id?: number;
  path?: string;
  status?: number;
  event_emitted?: boolean;
  pinned_root_stable?: boolean;
  // step 6 — bond resolve
  parent_eoa?: string;
  child_eoa?: string;
  child_budget_usdc?: number;
  constitution_hash_inherited?: string;
  bond_post?: BondPost;
  fund_oracle_tx?: string;
  record_advice?: RecordAdvice;
  error?: string;
  bond_resolved?: boolean;
  explorer_url?: string;
}

export interface RunStep {
  step: number;
  name: string;
  ok: boolean;
  duration_ms?: number;
  tx_hash?: string;
  explorer_url?: string;
  evidence: StepEvidence;
}

export interface RunResponse {
  ok: boolean;
  /** true when the file exists and has >= 6 steps */
  populated: boolean;
  steps: RunStep[];
  /** human-readable note when not populated */
  message?: string;
}
