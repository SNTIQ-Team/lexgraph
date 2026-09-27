# Source-bound decision passages

Implement paper.MD P1 decision-paragraph retrieval over the already retained RII XML, then expose it to Q_LWM's MCP. Decision metadata search remains a separate operation. No predictive-performance claim follows from corpus access.

Extract every top-level RspDL block within the original title/headnote/disposition/facts/reasons/dissent sections. A paragraph label is official only when present in dt with a source anchor; unnumbered blocks keep a representation-local block ID and null paragraph label. Preserve court section versus source headnote/other text roles without asserting that reported claims are findings. Bind decision identity, original ZIP/XML SHA256, XML path, source anchor, normalized UTF-8 text SHA256 and observation time. Do not backdate knowledge from decision dates or file mtimes.

SQLite stores passages, FTS5 and compressed original ZIPs once per decision. Search is bounded lexical AND over passages, with exact decision/section filters and pagination. Exact block reads preserve hashes; source downloads return the original ZIP. Unsupported temporal/unknown/repeated parameters fail explicitly. Absent index, missing source and malformed index are typed separately from zero matches. Snapshot preconditions use the existing Lexgraph contract.

Validate synthetic markers versus source numbering, multiline/table text, nested structure, duplicate anchors, mismatched identity, corruption/missing files and injection. Compare all retained XML paragraph anchors independently against exported keys; fetch production search/exact/source, verify hashes and packaged MCP process. Preserve existing dirty Lexgraph work; publish a new immutable corpus generation without copying large unchanged files.
