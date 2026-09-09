"""HTTP-boundary concerns: shape classification, serialisation, error mapping.

A package rather than modules hanging off `api/`, for the reason
`api/agent/tools.py` gives about itself: `main.py` stays a thin surface and the
logic that would otherwise accumulate there lives in domain modules.

Nothing in here may execute SQL. The boundary adapts what the agent produced;
it does not reach past `execute_sql()` (charter section 4).
"""
