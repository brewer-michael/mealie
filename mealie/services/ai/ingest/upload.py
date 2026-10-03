"""
`POST /api/ai/ingest`'s request handling (docs/ai/PHASE2.md §1.2): the checks made before any body byte is read, the
byte-capped body stream, and the three body shapes (multipart, a raw image, JSON with base64 images).

Work item B2 provides it.
"""
