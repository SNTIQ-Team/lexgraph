"""QFS v4 arena writer/reader (pure Python, zero deps).

Byte-faithful to qfs_visualizer/src/qfsParser.ts: a .qfs file is a single
sequence of 64-byte-aligned objects, each starting with

    QObjHeader { u32 tag; u32 flags; u64 size; u64 self; u8 hash[32] }  (56 B)

walked from the end of the 4096-byte superblock to `brk`. All references
between objects are byte offsets (QOff) — no pointers, so the file is
position-independent and the visualizer renders it as-is.

Object payloads implemented here match the parser's field offsets exactly;
files produced by QfsWriter load in the stock qfs_visualizer (nodes, edges
incl. hyperedges with roles, blobs, beliefs, states, transitions, worlds,
timeline via bornTick).

Semantic mapping used by Lexgraph (see docs/VISION.md):
    QNode      norm / act / institution / origin (label -> Blob)
    QEdge      AMENDS / IMPLEMENTS / REFERS_TO ... (delta, trust)
    QBelief    "norm is in force" claim: pTrue=gilt, pBoth=contested/
               pending, revisions = amendments, bornTick = event date
    QState     pipeline snapshot per legislative tick
    QTransition stage change (eingebracht -> beschlossen -> verkuendet)
    QWorld     jurisdiction state per tick (contradiction = overdue duties)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = 0x0053464351            # 'QCFS\0' little-endian
VERSION = 4
HEADER = 56
SUPER_SIZE = 4096

TAG_FREE, TAG_SUPER, TAG_DIR, TAG_CAS, TAG_BLOB = 0, 1, 2, 3, 4
TAG_NODE, TAG_EDGE, TAG_ADJ = 7, 8, 9
TAG_QBELIEF, TAG_QSTATE, TAG_QTRANS, TAG_QWORLD = 11, 12, 13, 14

MEMORY = {"ephemeral": 0, "working": 1, "long-term": 2,
          "dormant": 3, "forgotten": 4}


def align64(x: int) -> int:
    return (x + 63) & ~63


def _header(tag: int, size: int, self_off: int, flags: int = 0) -> bytes:
    return struct.pack("<IIQQ32s", tag, flags, size, self_off, b"\0" * 32)


class QfsWriter:
    """Append-only arena builder. All add_* methods return the object's
    byte offset (its identity everywhere else in the file)."""

    def __init__(self, arena_cap: int = 1 << 20):
        self.arena_cap = arena_cap
        self.chunks: list[bytes] = []
        self.off = SUPER_SIZE
        self.node_count = 0
        self.edge_count = 0
        self.next_node_id = 1
        self.next_edge_id = 1
        self.root_dir = 0
        self.cas_index = 0
        self._blob_cache: dict[bytes, int] = {}

    # -- low level ------------------------------------------------------
    def _emit(self, tag: int, payload: bytes, flags: int = 0) -> int:
        size = align64(HEADER + len(payload))
        off = self.off
        body = _header(tag, size, off, flags) + payload
        self.chunks.append(body + b"\0" * (size - len(body)))
        self.off += size
        return off

    # -- structural stubs (census fidelity; parser keeps them addressable)
    def add_dir(self) -> int:
        off = self._emit(TAG_DIR, b"")
        if not self.root_dir:
            self.root_dir = off
        return off

    def add_cas(self) -> int:
        off = self._emit(TAG_CAS, b"")
        if not self.cas_index:
            self.cas_index = off
        return off

    # -- content --------------------------------------------------------
    def add_blob(self, data: bytes | str) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8")
        if data in self._blob_cache:            # content-addressed reuse
            return self._blob_cache[data]
        # payload: u64 len at +0, data at align64(+8) => 8 pad bytes
        payload = struct.pack("<Q", len(data)) + data
        off = self._emit(TAG_BLOB, payload)
        self._blob_cache[data] = off
        return off

    def add_node(self, label: str | int = 0, *, trust: int = 3,
                 level: int = 0, degree: int = 0, subgraph: int = 0,
                 attrs: int = 0, embed: int = 0) -> int:
        label_off = self.add_blob(label) if isinstance(label, str) else label
        payload = struct.pack("<6Q3I", self.next_node_id, label_off, attrs,
                              0, subgraph, embed, level, trust, degree)
        self.next_node_id += 1
        self.node_count += 1
        return self._emit(TAG_NODE, payload)

    def add_edge(self, endpoints: list[tuple[int, float]], *,
                 reltype: int = 0, delta: float = 1.0, trust: int = 3,
                 wscalar: float = 1.0, weight: int = 0,
                 provenance: int = 0, subgraph: int = 0) -> int:
        """endpoints: [(node_off, role)] — role +1 source, -1 sink, 0 neutral.
        arity 2 renders as a link, arity>2 as a hyperedge hub with spokes."""
        arity = len(endpoints)
        fixed = struct.pack("<QIIddQQQQI", self.next_edge_id, arity, reltype,
                            delta, wscalar, weight, 0, provenance,
                            subgraph, trust)
        # endpoints start at align64(payload_off + 72); payload starts at
        # off+56, off is 64-aligned => absolute endpoint start = off + 128,
        # i.e. pad the fixed part (68 B) to 72 bytes.
        pad = b"\0" * (72 - len(fixed))
        eps = b"".join(struct.pack("<Qd", n, r) for n, r in endpoints)
        self.next_edge_id += 1
        self.edge_count += 1
        return self._emit(TAG_EDGE, fixed + pad + eps)

    def add_belief(self, *, claim_key: int, subject: int, relation: int,
                   obj: int, p_true: float, p_false: float, p_both: float,
                   p_none: float, born_tick: int, updated_tick: int | None = None,
                   confidence: float | None = None, contradiction: float | None = None,
                   evidence: int = 0, provenance: int = 0, prev_version: int = 0,
                   nxt: int = 0, evidence_weight: float = 1.0,
                   strength: float = 1.0, expires_at: int = 0,
                   memory: str = "working", source_trust: int = 3,
                   revision: int = 1, flags: int = 0,
                   uncertainty: float | None = None) -> int:
        if confidence is None:
            confidence = max(p_true, p_false)
        if contradiction is None:
            contradiction = p_both + min(p_true, p_false)
        if uncertainty is None:
            uncertainty = p_none
        payload = struct.pack(
            "<Q7Q9d3Q4I",
            claim_key, subject, relation, obj, evidence, provenance,
            prev_version, nxt,
            p_true, p_false, p_both, p_none, confidence, uncertainty,
            contradiction, evidence_weight, strength,
            born_tick, updated_tick if updated_tick is not None else born_tick,
            expires_at,
            MEMORY.get(memory, 1), source_trust, revision, flags)
        return self._emit(TAG_QBELIEF, payload)

    def add_state(self, *, state_id: int, tick: int, parent: int = 0,
                  branch_from: int = 0, focus: int = 0, belief_head: int = 0,
                  memory_head: int = 0, last_transition: int = 0,
                  self_model: int = 0, entropy: float = 0.0,
                  uncertainty: float = 0.0, contradiction: float = 0.0,
                  expected_utility: float = 0.0, information_gain: float = 0.0,
                  last_policy: int = 0, flags: int = 0,
                  assumptions: int = 0, shadow_head: int = 0) -> int:
        payload = struct.pack(
            "<9Q5d2I2Q",
            state_id, tick, parent, branch_from, focus, belief_head,
            memory_head, last_transition, self_model,
            entropy, uncertainty, contradiction, expected_utility,
            information_gain, last_policy, flags, assumptions, shadow_head)
        return self._emit(TAG_QSTATE, payload)

    def add_transition(self, *, from_state: int, to_state: int,
                       observation: int = 0, action: int = 0,
                       belief_written: int = 0, memory_written: int = 0,
                       provenance: int = 0, entropy_before: float = 0.0,
                       entropy_after: float = 0.0,
                       contradiction_before: float = 0.0,
                       contradiction_after: float = 0.0,
                       expected_utility: float = 0.0,
                       information_gain: float = 0.0,
                       policy: int = 0, flags: int = 0) -> int:
        payload = struct.pack(
            "<7Q6d2I",
            from_state, to_state, observation, action, belief_written,
            memory_written, provenance,
            entropy_before, entropy_after, contradiction_before,
            contradiction_after, expected_utility, information_gain,
            policy, flags)
        return self._emit(TAG_QTRANS, payload)

    def add_world(self, *, world_id: int, tick: int, parent: int = 0,
                  state: int = 0, observed_state: int = 0,
                  repair_target: int = 0, provenance: int = 0,
                  contradiction_level: float = 0.0, stability: float = 1.0,
                  truth_posterior: float = 1.0, source_trust: int = 3,
                  repair_strategy: int = 0, flags: int = 0,
                  prediction_error: float = 0.0, **floats) -> int:
        d = lambda k: float(floats.get(k, 0.0))          # noqa: E731
        payload = struct.pack(
            "<14Q21d Q6I".replace(" ", ""),
            world_id, tick, parent, state, 0, 0, observed_state,
            repair_target, 0, 0, 0, 0, provenance, 0,
            d("predicted_entropy"), d("predicted_uncertainty"),
            d("predicted_contradiction"), d("predicted_reward"),
            d("d_entropy"), d("d_uncertainty"), d("d_contradiction"),
            d("reward_bias"), prediction_error, d("prediction_error_ema"),
            d("reward_predicted"), d("reward_observed"), d("reward_error"),
            d("uncertainty"), contradiction_level, d("edge_confidence"),
            truth_posterior, d("replication"), d("compression_gain"),
            d("protocol_clean"), stability,
            0,                                            # repairCost (u64? no: f64 @280)
            repair_strategy, source_trust, flags, 0, 0, 0)
        return self._emit(TAG_QWORLD, payload)

    # -- finish ----------------------------------------------------------
    def to_bytes(self) -> bytes:
        brk = self.off
        sb_payload = struct.pack(
            "<QII8Q",
            MAGIC, VERSION, 4096, max(self.arena_cap, brk), brk,
            self.root_dir, self.cas_index,
            self.node_count, self.edge_count,
            self.next_node_id, self.next_edge_id)
        sb = _header(TAG_SUPER, SUPER_SIZE, 0) + sb_payload
        sb += b"\0" * (SUPER_SIZE - len(sb))
        return sb + b"".join(self.chunks)

    def write(self, path: str) -> int:
        data = self.to_bytes()
        with open(path, "wb") as f:
            f.write(data)
        return len(data)


# ---------------------------------------------------------------- reader

@dataclass
class Parsed:
    superblock: dict = field(default_factory=dict)
    nodes: dict = field(default_factory=dict)
    edges: dict = field(default_factory=dict)
    blobs: dict = field(default_factory=dict)
    beliefs: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)
    transitions: dict = field(default_factory=dict)
    worlds: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)

    def label(self, node_off: int) -> str:
        n = self.nodes.get(node_off)
        if not n or not n["label"]:
            return f"#{node_off}"
        b = self.blobs.get(n["label"])
        return b["data"].decode("utf-8", "replace") if b else f"#{node_off}"


TAG_NAMES = {0: "FREE", 1: "Superblock", 2: "Dir", 3: "CAS", 4: "Blob",
             7: "QNode", 8: "QEdge", 9: "Adj", 11: "QBelief", 12: "QState",
             13: "QTransition", 14: "QWorld"}


def parse_qfs(buf: bytes) -> Parsed:
    """Python port of qfsParser.ts — used for round-trip validation."""
    p = Parsed()
    u32 = lambda o: struct.unpack_from("<I", buf, o)[0]       # noqa: E731
    u64 = lambda o: struct.unpack_from("<Q", buf, o)[0]       # noqa: E731
    f64 = lambda o: struct.unpack_from("<d", buf, o)[0]       # noqa: E731

    if u32(0) != TAG_SUPER:
        raise ValueError("not a QFS file")
    s = HEADER
    p.superblock = dict(magic=u64(s), version=u32(s + 8), pageSize=u32(s + 12),
                        arenaCap=u64(s + 16), brk=u64(s + 24),
                        rootDir=u64(s + 32), casIndex=u64(s + 40),
                        nodeCount=u64(s + 48), edgeCount=u64(s + 56))
    if p.superblock["magic"] != MAGIC:
        raise ValueError("bad magic")

    off, brk = SUPER_SIZE, p.superblock["brk"]
    while off < brk:
        tag, _fl = u32(off), u32(off + 4)
        size, self_off = u64(off + 8), u64(off + 16)
        if size < HEADER or self_off != off:
            break
        po = off + HEADER
        name = TAG_NAMES.get(tag, str(tag))
        p.counts[name] = p.counts.get(name, 0) + 1
        if tag == TAG_NODE:
            p.nodes[off] = dict(id=u64(po), label=u64(po + 8),
                                level=u32(po + 48), trust=u32(po + 52),
                                degree=u32(po + 56))
        elif tag == TAG_EDGE:
            arity = u32(po + 8)
            eo = align64(po + 72)
            eps = [(u64(eo + i * 16), f64(eo + i * 16 + 8))
                   for i in range(arity)]
            p.edges[off] = dict(id=u64(po), arity=arity, reltype=u32(po + 12),
                                delta=f64(po + 16), trust=u32(po + 64),
                                endpoints=eps)
        elif tag == TAG_BLOB:
            ln = u64(po)
            do = align64(po + 8)
            p.blobs[off] = dict(len=ln, data=bytes(buf[do:do + ln]))
        elif tag == TAG_QBELIEF:
            p.beliefs[off] = dict(
                claimKey=u64(po), subject=u64(po + 8), relation=u64(po + 16),
                object=u64(po + 24), prevVersion=u64(po + 48),
                pTrue=f64(po + 64), pFalse=f64(po + 72), pBoth=f64(po + 80),
                pNone=f64(po + 88), contradiction=f64(po + 112),
                bornTick=u64(po + 136), memoryClass=u32(po + 160),
                sourceTrust=u32(po + 164), revision=u32(po + 168))
        elif tag == TAG_QSTATE:
            p.states[off] = dict(id=u64(po), tick=u64(po + 8),
                                 parent=u64(po + 16), focus=u64(po + 32),
                                 entropy=f64(po + 72),
                                 contradiction=f64(po + 88),
                                 lastPolicy=u32(po + 112))
        elif tag == TAG_QTRANS:
            p.transitions[off] = dict(fromState=u64(po), toState=u64(po + 8),
                                      beliefWritten=u64(po + 32),
                                      policy=u32(po + 104))
        elif tag == TAG_QWORLD:
            p.worlds[off] = dict(id=u64(po), tick=u64(po + 8),
                                 observedState=u64(po + 48),
                                 contradictionLevel=f64(po + 224),
                                 stability=f64(po + 272),
                                 sourceTrust=u32(po + 300))
        off += size
    return p


if __name__ == "__main__":                       # round-trip self-test
    w = QfsWriter()
    w.add_dir()
    w.add_cas()
    a = w.add_node("AsylbLG", trust=5)
    b = w.add_node("BGBl", trust=5)
    e = w.add_edge([(a, 1.0), (b, -1.0)], reltype=1, delta=0.45, trust=4)
    h = w.add_edge([(a, 1.0), (b, -1.0), (a, 0.0)], reltype=2)  # hyperedge
    verb = w.add_node("GILT")
    bl = w.add_belief(claim_key=1, subject=a, relation=verb, obj=b,
                      p_true=.88, p_false=.04, p_both=.02, p_none=.06,
                      born_tick=1, source_trust=5)
    st = w.add_state(state_id=1, tick=1, focus=a, entropy=.35)
    tr = w.add_transition(from_state=st, to_state=st, belief_written=bl,
                          policy=3)
    wd = w.add_world(world_id=1, tick=1, observed_state=st, stability=.9)
    data = w.to_bytes()
    p = parse_qfs(data)
    assert p.label(a) == "AsylbLG" and p.label(b) == "BGBl"
    assert p.edges[e]["delta"] == 0.45 and p.edges[e]["arity"] == 2
    assert p.edges[h]["arity"] == 3
    assert abs(p.beliefs[bl]["pTrue"] - .88) < 1e-12
    assert p.beliefs[bl]["contradiction"] == .02 + .04
    assert p.states[st]["tick"] == 1 and p.transitions[tr]["policy"] == 3
    assert p.worlds[wd]["stability"] == .9
    assert p.superblock["nodeCount"] == 3 and p.superblock["edgeCount"] == 2
    print(f"round-trip OK: {len(data)} bytes, counts={p.counts}")
