from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import MEMPALACE_KG_PATH, MEMPALACE_PATH

router = APIRouter(prefix="/memory")


def _collection():
    import chromadb
    from mempalace.config import MempalaceConfig
    client = chromadb.PersistentClient(path=MEMPALACE_PATH)
    return client.get_or_create_collection(
        name=MempalaceConfig().collection_name,
        metadata={"hnsw:space": "cosine"},
    )


@router.get("/search")
def search(q: str, limit: int = 10, domain: str | None = None):
    """Semantic search over ChromaDB memories."""
    if not q.strip():
        return JSONResponse({"error": "q is required"}, status_code=400)
    try:
        col     = _collection()
        where   = {"domain": domain} if domain else None
        kw      = {"where": where} if where else {}
        results = col.query(query_texts=[q], n_results=min(limit, 50), **kw)
        docs    = results.get("documents", [[]])[0]
        metas   = results.get("metadatas", [[]])[0]
        dists   = results.get("distances", [[]])[0]
        items   = [
            {"summary": d, "score": round(1 - dist, 3), **m}
            for d, m, dist in zip(docs, metas, dists)
        ]
        return {"results": items, "count": len(items)}
    except ImportError:
        return JSONResponse({"error": "chromadb / mempalace not installed"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/recent")
def recent(limit: int = 20, domain: str | None = None):
    """Most recent memories, optionally filtered by domain."""
    try:
        col     = _collection()
        where   = {"domain": domain} if domain else None
        kw      = {"where": where} if where else {}
        results = col.get(limit=min(limit, 100), include=["documents", "metadatas"], **kw)
        docs    = results.get("documents", [])
        metas   = results.get("metadatas", [])
        items   = [{"summary": d, **m} for d, m in zip(docs, metas)]
        items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return {"results": items, "count": len(items)}
    except ImportError:
        return JSONResponse({"error": "chromadb / mempalace not installed"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/kg")
def kg(subject: str = "aero", limit: int = 100):
    """Query knowledge graph triples for a given subject entity."""
    try:
        from mempalace.knowledge_graph import KnowledgeGraph
        store   = KnowledgeGraph(db_path=MEMPALACE_KG_PATH)
        triples = []
        for method in ("get_triples", "query_triples", "search_triples"):
            if hasattr(store, method):
                try:
                    triples = getattr(store, method)(subject=subject) or []
                    break
                except Exception:
                    pass
        return {"subject": subject, "triples": triples[:limit], "count": len(triples)}
    except ImportError:
        return JSONResponse({"error": "mempalace not installed"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
