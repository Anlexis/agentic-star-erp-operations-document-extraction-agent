"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# RET-C2-334 — Retail ERP & Operations Document Extraction Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Deterministic extraction pipeline
# (langextract-style rule/regex field extraction — NO LLM in v1).
# Fields below cover both layers.
#
# Cross-node contract (producer → consumer):
#   validated_input        : PreProcessNode (outer) → main GraphNode.extract_input
#   doc_type               : InputValidateNode      → DocumentParse/FieldExtract/ERPValidate/OutputValidate
#   doc_metadata           : InputValidateNode      → DocumentParseNode
#   parsed_fields          : DocumentParseNode       → FieldExtractNode
#   extracted_data         : FieldExtractNode        → ERPValidateNode, OutputValidateNode
#   erp_validation_result  : ERPValidateNode         → OutputValidateNode
#   formatted_output       : OutputValidateNode      → get_output → merge_output → PostProcessNode
#   out_of_scope           : InputValidateNode / OutputValidateNode → outer post_process
#
# Compliance note: extracted ERP fields (supplier codes, invoice numbers,
# amounts) fall under Denshi-Chobo-Hozon-Ho 2024 record-keeping rules.
# Account/transaction fields are masked upstream in PreProcessNode (S-1);
# OutputValidateNode re-runs the S-3 gate before surfacing any output.

from typing import Any, Dict, List, Optional, Tuple

from framework.schemas.agent_state import AgentState

#: The top-level keys the delivered payload always carries.
#:
#: Defined here rather than in either node, because BOTH ends of the delivery
#: path check it: OutputValidateNode backfills any key a branch did not set, and
#: PostProcessNode refuses to deliver a payload that is missing one. Two copies of
#: this tuple would let the producer and the gate drift into disagreeing about
#: what a complete payload is, and the gate would be the one that looked right.
REQUIRED_OUTPUT_KEYS: Tuple[str, ...] = (
    "template_id",
    "doc_type",
    "extracted_data",
    "erp_validation",
    "out_of_scope",
    "mandatory_fields_captured",
    "mandatory_fields_missing",
    "compliance_note",
)


class State(AgentState):
    """Flat TypedDict for RET-C2-334.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — PreProcessNode (pre_process slot, S-1 sanitization)
    # ------------------------------------------------------------------

    # Sanitized raw document text (or path) produced by the outer
    # PreProcessNode (S-1).  Read by the main GraphNode.extract_input()
    # and by InputValidateNode as the intake payload.
    validated_input: Optional[str]

    # The caller's structured parameters, normalized by
    # src/services/caller_contract.py: the ERP reference codes to cross-check
    # against and the reconciliation tolerance.  Written by PreProcessNode in
    # the OUTER graph and seeded into the INNER graph through the context
    # bridge, because the framework forwards only the request string across the
    # nested-graph boundary.  Read by ERPValidateNode.
    caller_contract: Optional[Dict[str, Any]]

    # Runtime tuning forwarded from config/config.yaml into the inner graph
    # (line-item cap, default reconciliation tolerance).  Node execute()
    # methods receive no config argument, so declared values have to travel as
    # state or they are declared and ignored.
    erp_runtime_config: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner domain workflow — InputValidateNode
    # ------------------------------------------------------------------

    # Detected document type: one of
    # "invoice" | "purchase_order" | "delivery_receipt" | "inventory_report".
    # Written by InputValidateNode; read by DocumentParseNode,
    # FieldExtractNode, ERPValidateNode and OutputValidateNode.
    doc_type: Optional[str]

    # Lightweight intake metadata: {"source": str, "char_len": int}.
    # Written by InputValidateNode; read by DocumentParseNode.
    doc_metadata: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner domain workflow — DocumentParseNode
    # ------------------------------------------------------------------

    # Raw doc-type-specific key-value pairs extracted from the document
    # structure (regex / line-anchored parse — NO LLM).
    # Written by DocumentParseNode; read by FieldExtractNode.
    parsed_fields: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner domain workflow — FieldExtractNode
    # ------------------------------------------------------------------

    # Normalized canonical ERP-compatible fields (doc_id, issue_date,
    # supplier_code, line_items[sku/quantity/unit/unit_price/line_total],
    # total_amount, currency, raw_fields).  Field names follow
    # Denshi-Chobo-Hozon-Ho standard naming where applicable.
    # Written by FieldExtractNode; read by ERPValidateNode + OutputValidateNode.
    extracted_data: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner domain workflow — ERPValidateNode
    # ------------------------------------------------------------------

    # ERP cross-check result: {sku_matches, supplier_match, date_in_range,
    # total_check, validation_flags, overall_valid}.
    # Written by ERPValidateNode; read by OutputValidateNode.
    erp_validation_result: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner domain workflow — OutputValidateNode (S-3 gate + S-4 audit)
    # ------------------------------------------------------------------

    # Final structured payload for the API response (template_id, doc_type,
    # extracted_data, erp_validation, out_of_scope, compliance_note).
    # S-3-gated before write.  Consumed by inner get_output() → outer
    # merge_output() → PostProcessNode.
    formatted_output: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Control / routing flags
    # ------------------------------------------------------------------

    # True when the input document is unparseable or its type is unknown,
    # as determined by InputValidateNode (or re-set by OutputValidateNode
    # if the output fails the structure check).  Out-of-scope is NOT a
    # separate AgentStatus — status stays SUCCESS and downstream nodes /
    # merge_output() inspect this flag.  Defaults to False.
    out_of_scope: bool

    # ------------------------------------------------------------------
    # Status and audit — populated by all nodes
    # ------------------------------------------------------------------

    # AgentStatus string value ("SUCCESS" or "ERROR") set by each node.
    # Declared explicitly so the outer merge_output() can read it from the
    # inner graph's merged state without an attribute-access miss.
    status: Optional[str]

    # Accumulated error messages appended by any node that catches a
    # system/infrastructure failure.
    error_log: List[str]

    # ------------------------------------------------------------------
    # Tracing / audit
    # ------------------------------------------------------------------

    # Correlation ID injected by InitializeNode for audit-log correlation.
    # OutputValidateNode includes session/trace context in emit_trace_event().
    trace_id: Optional[str]
