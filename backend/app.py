from pathlib import Path
from typing import Any, Dict, Optional


from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


from backend.config import BACKEND_DIR
from backend.database import KGRepository
from backend.pipeline.orchestrator import KGIterationOrchestrator


STATIC_DIR = BACKEND_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


app = FastAPI(
    title="His-Tag Protein Purification Knowledge Graph Explorer",
    description="多源融合与缺口驱动迭代的组氨酸标签蛋白纯化知识图谱系统",
    version="1.0.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


repo = KGRepository()
orchestrator = KGIterationOrchestrator(repo)


def _enrich_source_provenance(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    分离采集渠道 (collection_channel) 与出版来源 (publication_source) 及双链接，
    即使数据库中的旧记录未存 openalex_url，也能从 source_id (openalex_W...) 自动还原。
    """
    enriched = dict(item)
    meta = enriched.get("metadata") or {}
    source_type = enriched.get("source_type", "")
    # 兼容 /api/sources 的 "url" 和 /api/evidence/{edge_id} 的 "source_url"
    raw_url = enriched.get("url") or enriched.get("source_url") or ""
    source_id = enriched.get("source_id") or ""

    # 1. 提取或还原 OpenAlex ID 与 采集渠道链接
    oa_id = meta.get("openalex_id")
    if not oa_id and source_id.startswith("openalex_"):
        oa_id = source_id.replace("openalex_", "", 1)
    if not oa_id and "openalex.org/" in raw_url:
        oa_id = raw_url.rstrip("/").split("/")[-1]

    enriched["collection_channel"] = source_type
    if source_type == "OpenAlex":
        enriched["channel_url"] = (
            meta.get("openalex_url")
            or (f"https://openalex.org/{oa_id}" if oa_id else None)
            or (raw_url if "openalex.org" in raw_url else "https://openalex.org")
        )
    else:
        enriched["channel_url"] = raw_url or None

    # 2. 出版来源名称与出版商原文链接
    journal_name = meta.get("journal_name")
    publisher_name = meta.get("publisher_name")
    if source_type == "OpenAlex":
        if journal_name and publisher_name and journal_name.lower() != publisher_name.lower():
            enriched["publication_source"] = f"{journal_name} ({publisher_name})"
        else:
            enriched["publication_source"] = journal_name or publisher_name or "原始出版商 (DOI)"

        enriched["publisher_url"] = (
            meta.get("publisher_landing_url")
            or meta.get("doi_url")
            or (raw_url if raw_url and "openalex.org" not in raw_url else None)
        )
    else:
        enriched["publication_source"] = journal_name or publisher_name or source_type
        enriched["publisher_url"] = raw_url or None

    # 3. 免费 OA 全文链接（如有）
    enriched["oa_pdf_url"] = meta.get("oa_pdf_url")

    return enriched

    # 2. 出版来源信息（期刊 / 出版商 / 权威数据库）
    journal_name = meta.get("journal_name")
    publisher_name = meta.get("publisher_name")
    if source_type == "OpenAlex":
        if journal_name and publisher_name and journal_name.lower() != publisher_name.lower():
            enriched["publication_source"] = f"{journal_name} ({publisher_name})"
        else:
            enriched["publication_source"] = journal_name or publisher_name or "DOI 原始出版商"
    else:
        enriched["publication_source"] = journal_name or publisher_name or source_type

    # 3. 出版商原文链接与 OA 免费全文链接
    enriched["publisher_url"] = (
        meta.get("publisher_landing_url")
        or meta.get("doi_url")
        or (raw_url if "doi.org" in raw_url or "openalex.org" not in raw_url else None)
    )
    enriched["oa_pdf_url"] = meta.get("oa_pdf_url")

    # 4. 组合展示标签（例如: "OpenAlex · FEBS Letters (Wiley)"）
    if enriched["publication_source"] and enriched["publication_source"] != source_type:
        enriched["display_source_label"] = f"{source_type} · {enriched['publication_source']}"
    else:
        enriched["display_source_label"] = source_type

    return enriched


@app.get("/")
def serve_frontend():
    """直接托管单页交互前端"""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="前端页面 backend/static/index.html 尚未创建")
    return FileResponse(str(index_file))


@app.get("/api/graph")
def get_graph(
    protein: Optional[str] = Query(default=None, description="按蛋白名称或UniProt ID筛选"),
    method: Optional[str] = Query(default=None, description="按纯化方法筛选"),
    max_round: Optional[int] = Query(default=None, description="筛选截至第N轮的图谱状态"),
):
    """获取过滤后的知识图谱节点、带证据边及可用的蛋白/方法筛选下拉选项"""
    graph_data = repo.get_full_graph(
        protein_filter=protein if protein and protein != "ALL" else None,
        method_filter=method if method and method != "ALL" else None,
        max_round=max_round,
    )

    # 同时提取全量蛋白与纯化方法列表供前端筛选框使用
    full = repo.get_full_graph()
    proteins = sorted(
        [
            {"node_id": n["node_id"], "label": n["label"]}
            for n in full["nodes"]
            if n["node_type"] == "Protein"
        ],
        key=lambda x: x["label"],
    )
    methods = sorted(
        [
            {"node_id": n["node_id"], "label": n["label"]}
            for n in full["nodes"]
            if n["node_type"] == "PurificationMethod"
        ],
        key=lambda x: x["label"],
    )

    resins = sorted(
        [
            {"node_id": n["node_id"], "label": n["label"]}
            for n in full["nodes"]
            if n["node_type"] == "ResinMaterial"
        ],
        key=lambda x: x["label"],
    )
    return {
        "nodes": graph_data["nodes"],
        "edges": graph_data["edges"],
        "stats": graph_data["stats"],
        "filters": {
            "proteins": proteins,
            "methods": methods,
            "resins": resins,
        },
    }


@app.get("/api/evidence/{edge_id}")
def get_edge_evidence(edge_id: str):
    """点击图谱关系连线时，返回该关系的逐字原文引证、完整文档摘要与原始来源URL"""
    evidence = repo.get_edge_evidence(edge_id)
    if not evidence:
        raise HTTPException(status_code=404, detail=f"未找到关系 {edge_id} 的证据记录")

    # 兼容 evidence 为字典（含 evidences 列表或扁平字段）以及纯列表两种返回结构
    if isinstance(evidence, dict):
        if "evidences" in evidence and isinstance(evidence["evidences"], list):
            evidence["evidences"] = [_enrich_source_provenance(e) for e in evidence["evidences"]]
        else:
            evidence = _enrich_source_provenance(evidence)
    elif isinstance(evidence, list):
        evidence = [_enrich_source_provenance(e) for e in evidence]

    return evidence


@app.get("/api/sources")
def get_sources():
    """获取已入库的 UniProt / Cytiva / OpenAlex 全部去重原始资料与链接"""
    sources = repo.get_all_sources()
    enriched_sources = [_enrich_source_provenance(s) for s in sources]
    return {
        "total": len(enriched_sources),
        "sources": enriched_sources,
    }


@app.get("/api/logs")
def get_iteration_logs():
    """获取各轮迭代的检索理由、新增资料统计、新增实体/关系及属性补全/修正明细"""
    logs = repo.get_iteration_logs()
    gaps = repo.get_unresolved_gaps()
    return {
        "logs": logs,
        "unresolved_gaps": gaps,
    }


@app.post("/api/iterate/{round_num}")
def trigger_iteration(round_num: int):
    """点击触发指定轮次（第1轮、第2轮、第3轮及后续收敛验证轮）"""
    if round_num < 1 or round_num > 10:
        raise HTTPException(status_code=400, detail="轮次编号必须在 1 到 10 之间")
    try:
        result = orchestrator.run_iteration(round_num=round_num)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"执行第 {round_num} 轮迭代失败: {str(exc)}")


@app.post("/api/reset")
def reset_knowledge_graph():
    """清空当前数据库，方便从零重新演示三轮迭代"""
    repo.reset_database()
    return {"status": "OK", "message": "知识图谱与迭代日志已清空重置"}


# 挂载静态资源目录
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")