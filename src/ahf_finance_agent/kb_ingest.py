"""Build the policy-document vector index (build step 4).

Reads every ``*.md`` / ``*.txt`` under ``knowledge_base/docs/`` (override with
``KB_DOCS_DIR``), chunks each file by heading + size, embeds the chunks (AI Core
if ``EMBEDDING_DEPLOYMENT_ID`` is set, else the local fallback embedding) and
writes the index to ``KB_INDEX_PATH`` (default ``knowledge_base/index.json``).

Run it whenever the source documents change:

    python -m ahf_finance_agent.kb_ingest
    python -m ahf_finance_agent.kb_ingest --docs ./my-sops --out ./kb.json

The server also runs :func:`build_index` on startup when the index file is
missing or ``KB_REBUILD_ON_START`` is set, so a fresh ``cf push`` ships a
working knowledge base without a separate deploy task.

Document *quality* — not this script — is the hard part: outdated or conflicting
policy text is retrieved and repeated verbatim by the bot. Curating the source
files is a finance-stakeholder task; this just indexes whatever is in the folder.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ahf_finance_agent.config import get_settings
from ahf_finance_agent.knowledge_base import (
    Chunk,
    VectorIndex,
    chunk_document,
    embedding_backend_name,
)

logger = logging.getLogger("ahf_finance_agent.kb_ingest")


def _title_from(path: Path, text: str) -> str:
    for line in text.splitlines():
        h = line.strip()
        if h.startswith("# "):
            return h[2:].strip()
    return path.stem.replace("_", " ").replace("-", " ").title()


def collect_chunks(docs_dir: str) -> list[Chunk]:
    root = Path(docs_dir)
    if not root.exists():
        raise FileNotFoundError(f"Docs directory not found: {root.resolve()}")
    files = sorted(
        p for p in root.rglob("*")
        if p.suffix.lower() in {".md", ".txt"}
        and p.name.lower() != "readme.md"
        and not p.name.startswith("_")
    )
    if not files:
        raise FileNotFoundError(f"No indexable .md or .txt files under {root.resolve()}")
    chunks: list[Chunk] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = str(path.relative_to(root))
        doc_chunks = chunk_document(text, source=rel, title=_title_from(path, text))
        logger.info("Indexed %s -> %d chunks", rel, len(doc_chunks))
        chunks.extend(doc_chunks)
    return chunks


def build_index(docs_dir: str | None = None) -> int:
    """Chunk + embed + write the index. Returns the chunk count."""
    s = get_settings()
    docs_dir = docs_dir or s.kb_docs_dir
    chunks = collect_chunks(docs_dir)
    logger.info(
        "Embedding %d chunks via %s backend -> %s",
        len(chunks), embedding_backend_name(), s.kb_index_path,
    )
    VectorIndex().build(chunks)
    logger.info("Done. %d chunks written to %s", len(chunks), s.kb_index_path)
    return len(chunks)


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Build the AHF finance policy-doc vector index.")
    parser.add_argument("--docs", default=s.kb_docs_dir)
    parser.add_argument("--out", default=s.kb_index_path)
    args = parser.parse_args(argv)

    # --out overrides the configured index path for this run.
    if args.out != s.kb_index_path:
        s.kb_index_path = args.out

    try:
        build_index(args.docs)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
