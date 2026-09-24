"""Retrieval-augmented grounding for AP / procurement / finance policy questions.

Build step 4. This is the retrieval half of the policy knowledge base; the
``search_policy_docs`` tool (:mod:`ahf_finance_agent.tools`) is the half the
model calls.

* :func:`embed_texts` — turns text into vectors. Uses an SAP AI Core / GenAI
  Hub embedding deployment when ``EMBEDDING_DEPLOYMENT_ID`` is set (production
  path — embeddings stay inside SAP's data estate), and falls back to a small
  deterministic local embedding otherwise so the pipeline, the tests, and a
  laptop demo all work with zero cloud credentials.

* :class:`VectorIndex` — the store. Two backends behind one interface:
    - ``local`` : a JSON-lines file of chunks + vectors, cosine-ranked in pure
      Python. The POC / CI default (``KB_BACKEND=local``).
    - ``hana``  : SAP HANA Cloud vector store. Stub with wiring instructions —
      the production target once a HANA Cloud instance is bound
      (``KB_BACKEND=hana``).

* :meth:`VectorIndex.retrieve` — top-k chunks for a query, each with a
  similarity score, plus a ``grounded`` flag driven by ``KB_MIN_SCORE``. The
  ``search_policy_docs`` tool uses ``grounded=False`` as the signal to hand off
  to a human rather than answer.

Nothing here imports langchain / langgraph or the LLM client, so it stays
unit-testable in a bare environment and cheap to import.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ahf_finance_agent.config import get_settings

logger = logging.getLogger(__name__)

_LOCAL_EMBED_DIM = 512

# Dropped from the local fallback embedding so common filler words don't create
# spurious similarity between unrelated texts (a real embedding model handles
# this itself). Not used when EMBEDDING_DEPLOYMENT_ID is set.
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have how i if in is it its of on or
    that the this to was what when where which who will with you your do does did
    can could should would may might must our we they them their than then so not
    no yes any all each per about into out up down over under again more most
    some such only own same very s t don
    policy policies process processes procedure procedures question questions
    guidance information info detail details please thanks thank hello hey need
    want tell explain know find get use work working standard""".split()
)


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
def _local_embed_one(text: str) -> list[float]:
    """Deterministic hashing bag-of-words embedding — no model, no network.

    Not competitive with a real embedding model, but stable and dependency-free:
    enough to prove retrieval + grounding end-to-end and to run in CI.
    """
    vec = [0.0] * _LOCAL_EMBED_DIM
    tokens = [
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 2 and t not in _STOPWORDS
    ]
    keys: list[str] = []
    for tok in tokens:
        # Token + 4-char prefix (a crude stem) so "invoices"/"invoice" share a
        # dimension.
        keys.append(tok)
        keys.append("_p:" + tok[:4])
    # Adjacent-token bigrams: "payment terms", "onboard vendor" carry more
    # signal than the unigrams alone.
    for a, b in zip(tokens, tokens[1:]):
        keys.append("_b:" + a + " " + b)

    for key in keys:
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        idx = h % _LOCAL_EMBED_DIM
        sign = 1.0 if (h >> 8) & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _aicore_embed(texts: list[str], deployment_id: str) -> list[list[float]]:
    """Call an AI Core embedding deployment's OpenAI-compatible ``/embeddings``
    route through the ``GENAICORE`` BTP destination.

    Same connectivity rules as :mod:`ahf_finance_agent.llm`: the destination URL
    already ends in ``/v2``; the path is
    ``{url}/inference/deployments/{id}/embeddings``; AI Core's GPT/embedding
    deployments proxy to Azure OpenAI so every request needs an ``api-version``
    query param and the ``AI-Resource-Group`` header.
    """
    import httpx

    from ahf_finance_agent.btp import resolve_destination

    s = get_settings()
    dest = resolve_destination(s.aicore_destination_name)
    headers = {k: v for k, v in dest.headers.items() if k.lower() != "accept"}
    headers["AI-Resource-Group"] = s.aicore_resource_group
    url = f"{dest.url}/inference/deployments/{deployment_id}/embeddings"

    resp = httpx.post(
        url,
        headers=headers,
        params={"api-version": s.aicore_api_version},
        json={"input": texts, "model": s.embedding_model_name},
        timeout=60.0,
    )
    if resp.is_error:
        logger.error("AI Core embeddings failed: status=%s body=%s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    payload = resp.json()
    return [row["embedding"] for row in payload["data"]]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Prefers AI Core; falls back to the local embedding."""
    if not texts:
        return []
    deployment_id = get_settings().embedding_deployment_id
    if deployment_id:
        try:
            return _aicore_embed(texts, deployment_id)
        except Exception:
            logger.exception("AI Core embedding failed; falling back to local embedding")
    return [_local_embed_one(t) for t in texts]


def embedding_backend_name() -> str:
    return "aicore" if get_settings().embedding_deployment_id else "local"


def _content_words(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 3 and t not in _STOPWORDS
    }


def _lexical_overlap(query: str, passage: str) -> bool:
    """True if the query and passage share a content word (or a 5-char stem)."""
    q = _content_words(query)
    if not q:
        return False
    p = _content_words(passage)
    if q & p:
        return True
    q_stems = {w[:5] for w in q}
    p_stems = {w[:5] for w in p}
    return bool(q_stems & p_stems)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
@dataclass
class Chunk:
    id: str
    text: str
    source: str
    title: str
    section: str


def chunk_document(text: str, source: str, title: str, max_chars: int = 1100, overlap: int = 150) -> list[Chunk]:
    """Split a markdown / plain-text doc into overlapping chunks, keeping the
    nearest preceding heading as the chunk's ``section`` for citation.
    """
    lines = text.splitlines()
    chunks: list[Chunk] = []
    current_section = ""
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        body = "\n".join(buf).strip()
        if body:
            cid = hashlib.md5(f"{source}:{len(chunks)}:{body[:40]}".encode()).hexdigest()[:12]
            chunks.append(Chunk(id=cid, text=body, source=source, title=title, section=current_section))
        buf, buf_len = [], 0

    for line in lines:
        heading = re.match(r"^#{1,6}\s+(.*)", line)
        if heading:
            flush()
            current_section = heading.group(1).strip()
            continue
        buf.append(line)
        buf_len += len(line) + 1
        if buf_len >= max_chars:
            flush()
            if overlap and chunks:
                tail = chunks[-1].text[-overlap:]
                buf = [tail]
                buf_len = len(tail)
    flush()
    return chunks


# --------------------------------------------------------------------------- #
# Index / backends
# --------------------------------------------------------------------------- #
@dataclass
class Retrieved:
    text: str
    source: str
    title: str
    section: str
    score: float


class _LocalJSONBackend:
    """Chunks + vectors in a JSON-lines file, cosine-ranked in pure Python."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._rows: list[dict] = []
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            lines = [l for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]
            header = json.loads(lines[0]) if lines else {}
            if "chunks" in header:  # legacy single-object format
                self._rows = header.get("chunks", [])
            else:
                self._rows = [json.loads(l) for l in lines[1:]]
            logger.info(
                "KB local index loaded: path=%s chunks=%d embed_backend=%s",
                self.path, len(self._rows), header.get("embedding_backend", "?"),
            )
        else:
            logger.warning("KB local index not found at %s — retrieval will return nothing", self.path)
            self._rows = []
        self._loaded = True

    def is_empty(self) -> bool:
        self.load()
        return not self._rows

    def write(self, chunks: list[Chunk], vectors: list[list[float]], embed_backend: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{**asdict(c), "embedding": v} for c, v in zip(chunks, vectors)]
        # One JSON object per line: compact in git diffs, still greppable.
        lines = [json.dumps({"embedding_backend": embed_backend}, ensure_ascii=False)]
        lines += [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows]
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._rows, self._loaded = rows, True

    def query(self, vector: list[float], k: int) -> list[Retrieved]:
        self.load()
        scored = [
            Retrieved(
                text=r["text"], source=r["source"], title=r.get("title", ""),
                section=r.get("section", ""), score=_cosine(vector, r["embedding"]),
            )
            for r in self._rows
        ]
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:k]


_HANA_MSG = (
    "KB_BACKEND=hana is a stub. Provision HANA Cloud + Vector Engine, bind it, "
    "and implement _HanaCloudBackend. Until then run with KB_BACKEND=local "
    "(the default)."
)


class _HanaCloudBackend:
    """SAP HANA Cloud vector store backend — production target, not yet wired.

    To implement:
      1. Provision a HANA Cloud instance with the Vector Engine and bind it
         (service key -> HANA_VECTOR_SERVICE_KEY) or add a BTP destination.
      2. ``pip install hdbcli`` (or ``hana_ml`` / ``langchain-hana``).
      3. Create a table with a ``REAL_VECTOR(<dim>)`` column; upsert chunks in
         ``write()``; in ``query()`` run
             SELECT text, source, title, section,
                    COSINE_SIMILARITY(embedding, TO_REAL_VECTOR(:qv)) AS score
             FROM AHF_FINANCE_KB ORDER BY score DESC LIMIT :k
      The rest of this module (chunking, embed_texts, retrieve, the tool, the
      grounding threshold) is backend-agnostic and needs no change.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self):  # pragma: no cover - stub
        raise NotImplementedError(_HANA_MSG)

    def is_empty(self):  # pragma: no cover - stub
        raise NotImplementedError(_HANA_MSG)

    def write(self, *a, **k):  # pragma: no cover - stub
        raise NotImplementedError(_HANA_MSG)

    def query(self, *a, **k):  # pragma: no cover - stub
        raise NotImplementedError(_HANA_MSG)


def _make_backend() -> "_LocalJSONBackend | _HanaCloudBackend":
    s = get_settings()
    if s.kb_backend.lower() == "hana":
        return _HanaCloudBackend(s.kb_index_path)
    return _LocalJSONBackend(s.kb_index_path)


class VectorIndex:
    """Facade over whichever backend is configured."""

    def __init__(self) -> None:
        self._backend = _make_backend()

    def is_empty(self) -> bool:
        try:
            return self._backend.is_empty()
        except NotImplementedError:
            logger.error(_HANA_MSG)
            return True

    def build(self, chunks: list[Chunk]) -> None:
        vectors = embed_texts([c.text for c in chunks])
        self._backend.write(chunks, vectors, embedding_backend_name())

    def chunk_count(self) -> int:
        try:
            self._backend.load()
        except Exception:  # pragma: no cover - defensive (hana stub / unreadable file)
            return 0
        return len(getattr(self._backend, "_rows", []))

    def retrieve(
        self, query: str, k: int | None = None, min_score: float | None = None
    ) -> tuple[list[Retrieved], bool]:
        """Return ``(hits, grounded)``.

        ``grounded`` requires BOTH:
        * the top hit's similarity score >= ``min_score`` (``KB_MIN_SCORE``), and
        * the top hit shares at least one meaningful word with the query.

        The lexical check is a cheap guard against the local fallback
        embedding's habit of scoring a topically-unrelated chunk just above the
        threshold; a real embedding model clears the bar on score alone but
        still passes this.
        """
        s = get_settings()
        k = k or s.kb_top_k
        min_score = s.kb_min_score if min_score is None else min_score
        try:
            if self._backend.is_empty():
                return [], False
            qv = embed_texts([query])[0]
            hits = self._backend.query(qv, k)
        except NotImplementedError:
            logger.error(_HANA_MSG)
            return [], False
        grounded = (
            bool(hits)
            and hits[0].score >= min_score
            and _lexical_overlap(query, hits[0].text + " " + hits[0].section + " " + hits[0].title)
        )
        return hits, grounded


_INDEX_SINGLETON: VectorIndex | None = None


def get_index() -> VectorIndex:
    global _INDEX_SINGLETON
    if _INDEX_SINGLETON is None:
        _INDEX_SINGLETON = VectorIndex()
    return _INDEX_SINGLETON


def reset_index_cache() -> None:
    """Test hook: force the next :func:`get_index` to re-read config / files."""
    global _INDEX_SINGLETON
    _INDEX_SINGLETON = None
