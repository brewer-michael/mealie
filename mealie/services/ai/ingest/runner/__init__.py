"""
The recipe card runner (docs/ai/PHASE2.md §3.2-§3.9): a dispatcher per worker process that claims queued tasks from
the job table, runs each in a daemon thread with its own event loop, keeps their leases alive and applies their
results with fenced writes. `types` is the contract between the runner and the task handlers in
`mealie.services.ai.ingest.tasks`.
"""
