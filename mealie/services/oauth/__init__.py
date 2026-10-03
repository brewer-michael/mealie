"""
Fork-owned OAuth 2 authorization server for the MCP server (docs/ai/PHASE3.md §4): hand-written and minimal,
covering only what MCP clients (Home Assistant, Claude) need. Each rule cites the RFC section it implements.

Keep this module free of imports: the MCP verifier and the schemas import its leaf modules.
"""
