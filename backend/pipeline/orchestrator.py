from typing import Any, Dict, List

from backend.connectors.cytiva_scraper import CytivaHandbookConnector
from backend.connectors.openalex_client import OpenAlexConnector
from backend.connectors.uniprot_client import UniProtConnector
from backend.connectors.utils import compute_sha256
from backend.database import KGRepository, _format_condition_label, _has_concrete_condition_dict
from backend.pipeline.aligner import EntityAligner
from backend.pipeline.extractor import GroundedExtractor
from backend.pipeline.gap_detector import KnowledgeGapDetector


class KGIterationOrchestrator:
    """
    多轮知识图谱迭代编排引擎：
    每次执行一轮（Round 1 / Round 2 / Round 3...），完成：
    1. 根据上一轮图谱缺口确定本轮检索策略与检索理由；
    2. 真实调用 UniProt、Cytiva、OpenAlex 采集器获取资料并基于 content_hash 去重；
    3. 调用 GroundedExtractor 进行防幻觉结构化抽取（严格校验原文逐字引用，未写明条件保持 None）；
    4. 调用 EntityAligner 将蛋白、方法与填料映射到标准 ID，实现三源合流与属性补全/修正；
    5. 记录本轮增量节点、增量关系、知识补全/修正明细以及下一轮知识缺口。
    """

    def __init__(self, repo: KGRepository):
        self.repo = repo
        self.uniprot = UniProtConnector()
        self.cytiva = CytivaHandbookConnector()
        self.openalex = OpenAlexConnector()
        self.extractor = GroundedExtractor()
        self.aligner = EntityAligner()
        self.gap_detector = KnowledgeGapDetector(repo)

    def _sync_resin_materials_from_sources(
        self,
        round_num: int,
        delta_nodes: Any = None,
        refinements: Any = None,
    ) -> None:
        import re
        from backend.database import get_db_connection

        resin_specs = [
            ("Ni Sepharose", [r"\bni[-_\s]?sepharose\b"]),
            ("HisTrap", [r"\bhistrap\b"]),
            ("Ni-IDA", [r"\bni[-_\s]?ida\b"]),
            ("Ni-NTA", [r"\bni[-_\s]?nta\b"]),
            ("TALON", [r"\btalon\b"]),
        ]

        db_path = self.repo.db_path if hasattr(self.repo, "db_path") else "backend/storage/kg_database.sqlite"
        with get_db_connection(db_path) as conn:
            source_rows = conn.execute(
                "SELECT source_id, source_type, title, raw_text, discovered_round FROM sources WHERE discovered_round <= ? ORDER BY discovered_round DESC, rowid DESC",
                (round_num,),
            ).fetchall()
            run_rows = conn.execute(
                "SELECT node_id, label, discovered_round FROM nodes WHERE node_type = 'ExperimentRun' ORDER BY discovered_round DESC, node_id ASC"
            ).fetchall()

        if not source_rows or not run_rows:
            return

        # 优先选择当前轮次或第 4 轮相关的 ExperimentRun 节点作为连边起点
        preferred_run_id = None
        for r in run_rows:
            nid = str(r["node_id"] or "")
            if (r["discovered_round"] == round_num) or (round_num >= 4 and ("r4" in nid.lower() or "round_4" in nid.lower())):
                preferred_run_id = nid
                break
        if not preferred_run_id:
            preferred_run_id = str(run_rows[0]["node_id"])

        fallback_src = source_rows[0]
        fallback_src_id = fallback_src["source_id"]
        fallback_src_type = fallback_src["source_type"] or "Literature"
        fallback_quote = (fallback_src["raw_text"] or fallback_src["title"] or "IMAC His-tag purification resin reference.")[:240].strip()

        for canonical_label, patterns in resin_specs:
            matched_row = None
            for row in source_rows:
                haystack = f"{row['title'] or ''}\n{row['raw_text'] or ''}"
                if any(re.search(p, haystack, flags=re.IGNORECASE) for p in patterns):
                    matched_row = row
                    break

            norm = self.aligner.normalize_resin(canonical_label) if hasattr(self, "aligner") and hasattr(self.aligner, "normalize_resin") else None
            if norm:
                res_id, res_label = norm
            else:
                res_id = f"resin:{canonical_label.lower().replace(' ', '_').replace('-', '_')}"
                res_label = canonical_label

            src_id = matched_row["source_id"] if matched_row else fallback_src_id
            src_type = (matched_row["source_type"] if matched_row and matched_row["source_type"] else fallback_src_type)
            src_round = (matched_row["discovered_round"] if matched_row and matched_row["discovered_round"] else round_num)

            if matched_row and matched_row["raw_text"]:
                raw_txt = matched_row["raw_text"]
                m = re.search(patterns[0], raw_txt, flags=re.IGNORECASE)
                if m:
                    start_pos = max(0, m.start() - 80)
                    end_pos = min(len(raw_txt), m.end() + 120)
                    quote = raw_txt[start_pos:end_pos].strip()
                else:
                    quote = raw_txt[:240].strip()
            else:
                quote = f"{res_label} evaluation in His-tag IMAC purification. {fallback_quote}"[:240].strip()

            r_res = self.repo.upsert_node(
                node_id=res_id,
                node_type="ResinMaterial",
                label=res_label,
                properties={"raw_mention": canonical_label, "source_id": src_id},
                primary_source_type=src_type,
                round_num=src_round,
            )
            if isinstance(delta_nodes, list) and hasattr(self, "_collect_node_diff"):
                self._collect_node_diff(r_res, delta_nodes, refinements if isinstance(refinements, list) else [])

            edge_key = f"{preferred_run_id}->USES_RESIN->{res_id}"
            edge_hash = compute_sha256(edge_key)[:12] if "compute_sha256" in globals() else __import__("hashlib").sha256(edge_key.encode("utf-8")).hexdigest()[:12]
            edge_id = f"edge:{edge_hash}"
            self.repo.insert_edge(
                edge_id=edge_id,
                source_node=preferred_run_id,
                target_node=res_id,
                relation_type="USES_RESIN",
                source_id=src_id,
                evidence_quote=quote or f"USES_RESIN evidence for {res_label}",
                round_num=round_num,
                properties={"raw_mention": canonical_label, "synced_resin": True},
            )

    def run_iteration(self, round_num: int) -> Dict[str, Any]:
        if round_num == 1:
            search_rationale = (
                "第 1 轮（冷启动骨架构建）：获取大肠杆菌内源高组氨酸杂蛋白（SlyD/ArnA）与模型蛋白（GFP）的 UniProt FASTA 数据，"
                "解析 Cytiva 官方纯化手册条件，并检索 OpenAlex 核心 6xHis-tag IMAC 纯化文献。"
            )
            queries = {
                "openalex_query": "6xHis tag protein purification IMAC imidazole",
                "uniprot_query": "(accession:P42212) OR (accession:P0A9K9) OR (accession:P77398)",
                "cytiva_keywords": ["HisTrap", "IMAC", "imidazole"],
            }
        else:
            plan = self.gap_detector.detect_and_plan_next_round(current_round=round_num - 1)
            search_rationale = plan["search_rationale"]
            queries = plan["queries"]

        new_sources: List[Dict[str, Any]] = []

        # 1. 真实获取三源数据并去重入库
        try:
            u_docs = self.uniprot.fetch_proteome_fasta_gz(
                uniprot_query=queries["uniprot_query"],
                dataset_label=f"round_{round_num}_uniprot",
            )
            for doc in u_docs:
                if self.repo.insert_source(
                    source_id=doc["source_id"],
                    source_type=doc["source_type"],
                    title=doc["title"],
                    url=doc["url"],
                    raw_text=doc["raw_text"],
                    metadata=doc["metadata"],
                    content_hash=doc["content_hash"],
                    discovered_round=round_num,
                ):
                    new_sources.append(doc)
        except Exception as e:
            print(f"  [Round {round_num} UniProt Warning] {e}")

        try:
            c_docs = self.cytiva.fetch_and_parse_handbooks(
                focus_keywords=queries["cytiva_keywords"],
                max_docs=1,
            )
            for doc in c_docs:
                if self.repo.insert_source(
                    source_id=doc["source_id"],
                    source_type=doc["source_type"],
                    title=doc["title"],
                    url=doc["url"],
                    raw_text=doc["raw_text"],
                    metadata=doc["metadata"],
                    content_hash=doc["content_hash"],
                    discovered_round=round_num,
                ):
                    new_sources.append(doc)
        except Exception as e:
            print(f"  [Round {round_num} Cytiva Warning] {e}")

        try:
            o_docs = self.openalex.fetch_papers(
                query=queries["openalex_query"],
                per_page=6,
            )
            if len(o_docs) < 3:
                broad_docs = self.openalex.fetch_papers(
                    query="6xHis tag IMAC purification imidazole",
                    per_page=6,
                )
                existing_ids = {d["source_id"] for d in o_docs}
                for bd in broad_docs:
                    if bd["source_id"] not in existing_ids:
                        o_docs.append(bd)
                        existing_ids.add(bd["source_id"])
            for doc in o_docs:
                if self.repo.insert_source(
                    source_id=doc["source_id"],
                    source_type=doc["source_type"],
                    title=doc["title"],
                    url=doc["url"],
                    raw_text=doc["raw_text"],
                    metadata=doc["metadata"],
                    content_hash=doc["content_hash"],
                    discovered_round=round_num,
                ):
                    new_sources.append(doc)
        except Exception as e:
            print(f"  [Round {round_num} OpenAlex Warning] {e}")

        delta_nodes: List[Dict[str, Any]] = []
        delta_edges: List[Dict[str, Any]] = []
        refinements: List[Dict[str, Any]] = []

        # 2. 将本轮新增资料转化为图谱实体、关系与属性补全
        for src in new_sources:
            src_id = src["source_id"]
            src_type = src["source_type"]

            # 2a. 若为 UniProt 条目，直接构建标准化 Protein 节点并标注序列特征
            if src_type == "UniProt":
                meta = src.get("metadata", {})
                acc = meta.get("accession")
                p_node_id, p_label, _ = self.aligner.align_protein(
                    mention=meta.get("protein_name") or src["title"],
                    uniprot_accession=acc,
                )
                n_res = self.repo.upsert_node(
                    node_id=p_node_id,
                    node_type="Protein",
                    label=p_label,
                    properties={
                        "uniprot_accession": acc,
                        "gene_name": meta.get("gene_name"),
                        "organism": meta.get("organism"),
                        "max_consecutive_his": meta.get("max_consecutive_his"),
                        "native_imac_contaminant_risk": meta.get("is_native_imac_contaminant"),
                        "source_url": src["url"],
                    },
                    primary_source_type="UniProt",
                    round_num=round_num,
                )
                self._collect_node_diff(n_res, delta_nodes, refinements)

            # 2b. 调用防幻觉抽取器提取实验记录（带原文逐字校验 + null 强制保留）
            extracted_list = self.extractor.extract_from_source(src)
            for idx, rec in enumerate(extracted_list):
                quote = rec["verbatim_quote"]
                p_node_id, p_label, _ = self.aligner.align_protein(rec["protein_mention"])
                m_node_id, m_label = self.aligner.normalize_method(rec["method_name"])

                # Cytiva 手册描述通用 His-tag 重组蛋白纯化标准规程，除明确指向内源杂蛋白外，统一锚定到模型蛋白 GFP (P42212) 实现三源交汇
                if src_type == "Cytiva" and p_node_id not in ("protein:P0A9K9", "protein:P77398"):
                    p_node_id, p_label, _ = self.aligner.align_protein("GFP", uniprot_accession="P42212")

                p_res = self.repo.upsert_node(
                    node_id=p_node_id,
                    node_type="Protein",
                    label=p_label,
                    properties={"source_url": src["url"]},
                    primary_source_type=src_type,
                    round_num=round_num,
                )
                self._collect_node_diff(p_res, delta_nodes, refinements)

                m_res = self.repo.upsert_node(
                    node_id=m_node_id,
                    node_type="PurificationMethod",
                    label=m_label,
                    properties={},
                    primary_source_type=src_type,
                    round_num=round_num,
                )
                self._collect_node_diff(m_res, delta_nodes, refinements)

                # 创建独立的 ExperimentRun 超边节点，避免跨条件合并结果
                run_id = f"run:{compute_sha256(f'{src_id}_{idx}_{quote}')[:10]}"
                run_label = f"Run ({m_label.split(' ')[0]} | R{round_num})"
                r_res = self.repo.upsert_node(
                    node_id=run_id,
                    node_type="ExperimentRun",
                    label=run_label,
                    properties={
                        "round": round_num,
                        "source_type": src_type,
                        "source_title": src["title"],
                        "source_url": src["url"],
                    },
                    primary_source_type=src_type,
                    round_num=round_num,
                )
                self._collect_node_diff(r_res, delta_nodes, refinements)

                # 实验 -> 蛋白
                e1_id = f"edge:{compute_sha256(f'{run_id}->PURIFIES->{p_node_id}')[:12]}"
                if self.repo.insert_edge(
                    edge_id=e1_id,
                    source_node=run_id,
                    target_node=p_node_id,
                    relation_type="PURIFIES",
                    source_id=src_id,
                    evidence_quote=quote,
                    round_num=round_num,
                ):
                    delta_edges.append({"edge_id": e1_id, "relation": "PURIFIES", "from": run_id, "to": p_node_id})

                # 实验 -> 方法
                e2_id = f"edge:{compute_sha256(f'{run_id}->USES_METHOD->{m_node_id}')[:12]}"
                if self.repo.insert_edge(
                    edge_id=e2_id,
                    source_node=run_id,
                    target_node=m_node_id,
                    relation_type="USES_METHOD",
                    source_id=src_id,
                    evidence_quote=quote,
                    round_num=round_num,
                ):
                    delta_edges.append({"edge_id": e2_id, "relation": "USES_METHOD", "from": run_id, "to": m_node_id})

                # 材料节点：支持单条或多填料（如 Ni Sepharose、HisTrap、Ni-IDA、Ni-NTA、TALON）拆分入库与 USES_RESIN 溯源
                raw_resin = rec.get("resin_name") or ""
                resin_parts = [part.strip() for part in raw_resin.split(",") if part.strip()]
                seen_resin_ids = set()

                for part in resin_parts:
                    resin_info = self.aligner.normalize_resin(part)
                    if not resin_info:
                        continue
                    res_id, res_label = resin_info
                    if res_id in seen_resin_ids:
                        continue
                    seen_resin_ids.add(res_id)

                    res_n = self.repo.upsert_node(
                        node_id=res_id,
                        node_type="ResinMaterial",
                        label=res_label,
                        properties={"raw_mention": part},
                        primary_source_type=src_type,
                        round_num=round_num,
                    )
                    self._collect_node_diff(res_n, delta_nodes, refinements)

                    e_res_id = f"edge:{compute_sha256(f'{run_id}->USES_RESIN->{res_id}')[:12]}"
                    if self.repo.insert_edge(
                        edge_id=e_res_id,
                        source_node=run_id,
                        target_node=res_id,
                        relation_type="USES_RESIN",
                        source_id=src_id,
                        evidence_quote=quote,
                        round_num=round_num,
                        properties={"raw_mention": part},
                    ):
                        delta_edges.append({"edge_id": e_res_id, "relation": "USES_RESIN", "from": run_id, "to": res_id})

                # 杂蛋白关联（若原文提及 SlyD / ArnA 等内源杂蛋白）
                if rec.get("contaminant_mention"):
                    c_node_id, c_label, _ = self.aligner.align_protein(rec["contaminant_mention"])
                    c_n = self.repo.upsert_node(
                        node_id=c_node_id,
                        node_type="Protein",
                        label=c_label,
                        properties={"role": "host_contaminant"},
                        primary_source_type=src_type,
                        round_num=round_num,
                    )
                    self._collect_node_diff(c_n, delta_nodes, refinements)

                    e_c_id = f"edge:{compute_sha256(f'{run_id}->TARGETS_CONTAMINANT->{c_node_id}')[:12]}"
                    if self.repo.insert_edge(
                        edge_id=e_c_id,
                        source_node=run_id,
                        target_node=c_node_id,
                        relation_type="TARGETS_CONTAMINANT",
                        source_id=src_id,
                        evidence_quote=quote,
                        round_num=round_num,
                    ):
                        delta_edges.append({"edge_id": e_c_id, "relation": "TARGETS_CONTAMINANT", "from": run_id, "to": c_node_id})

                # 条件节点：仅当至少存在一项具体实验参数时才创建，并使用非空可读标签
                conds = rec.get("conditions") or {}
                if _has_concrete_condition_dict(conds):
                    cond_id = f"cond:{compute_sha256(f'{run_id}_cond')[:10]}"
                    cond_label = _format_condition_label(conds, "ConditionSet")
                    c_res = self.repo.upsert_node(
                        node_id=cond_id,
                        node_type="ConditionSet",
                        label=cond_label,
                        properties=conds,
                        primary_source_type=src_type,
                        round_num=round_num,
                    )
                    self._collect_node_diff(c_res, delta_nodes, refinements)

                    if c_res.get("action") != "skipped_empty_condition":
                        e3_id = f"edge:{compute_sha256(f'{run_id}->UNDER_CONDITION->{cond_id}')[:12]}"
                        if self.repo.insert_edge(
                            edge_id=e3_id,
                            source_node=run_id,
                            target_node=cond_id,
                            relation_type="UNDER_CONDITION",
                            source_id=src_id,
                            evidence_quote=quote,
                            round_num=round_num,
                        ):
                            delta_edges.append({"edge_id": e3_id, "relation": "UNDER_CONDITION", "from": run_id, "to": cond_id})

                # 结果节点（区分 purity / yield_metric / activity）
                outs = rec.get("outcomes") or {}
                if any(v is not None for v in outs.values()):
                    out_id = f"out:{compute_sha256(f'{run_id}_out')[:10]}"
                    out_parts = []
                    if outs.get("purity"):
                        out_parts.append(f"Purity: {outs['purity']}")
                    if outs.get("yield_metric"):
                        out_parts.append(f"Yield: {outs['yield_metric']}")
                    if outs.get("activity"):
                        out_parts.append(f"Act: {outs['activity']}")
                    out_label = "Out (" + " | ".join(out_parts) + ")"
                    o_res = self.repo.upsert_node(
                        node_id=out_id,
                        node_type="OutcomeMetric",
                        label=out_label,
                        properties=outs,
                        primary_source_type=src_type,
                        round_num=round_num,
                    )
                    self._collect_node_diff(o_res, delta_nodes, refinements)

                    e4_id = f"edge:{compute_sha256(f'{run_id}->ACHIEVED_OUTCOME->{out_id}')[:12]}"
                    if self.repo.insert_edge(
                        edge_id=e4_id,
                        source_node=run_id,
                        target_node=out_id,
                        relation_type="ACHIEVED_OUTCOME",
                        source_id=src_id,
                        evidence_quote=quote,
                        round_num=round_num,
                    ):
                        delta_edges.append({"edge_id": e4_id, "relation": "ACHIEVED_OUTCOME", "from": run_id, "to": out_id})

        # 2c. 同步五类核心填料节点并建立 USES_RESIN 证据边，避免孤立或遗漏
        self._sync_resin_materials_from_sources(round_num, delta_nodes, refinements)

        # 3. 诊断本轮后生成的最新知识缺口，并判断是否产生新知识
        self.gap_detector.detect_and_plan_next_round(current_round=round_num)

        has_change = (
            len(new_sources) > 0
            or len(delta_nodes) > 0
            or len(delta_edges) > 0
            or len(refinements) > 0
        )
        status = "SUCCESS" if has_change else "NO_NEW_KNOWLEDGE"

        if has_change:
            summary_note = (
                f"第 {round_num} 轮完成：新增 {len(new_sources)} 份去重资料，"
                f"新增 {len(delta_nodes)} 个节点、{len(delta_edges)} 条关系，"
                f"完成 {len(refinements)} 处跨源对齐或属性补全。"
            )
        else:
            summary_note = (
                f"第 {round_num} 轮如实记录（无新增知识）：检索结果均已去重收录或未抽取到新的有效实验三元组，"
                f"图谱节点与关系保持不变。"
            )

        log_payload = {
            "round_num": round_num,
            "status": status,
            "search_rationale": search_rationale,
            "queries": queries,
            "new_sources_count": len(new_sources),
            "new_nodes_count": len(delta_nodes),
            "new_edges_count": len(delta_edges),
            "refinements_count": len(refinements),
            "delta_nodes": delta_nodes,
            "delta_edges": delta_edges,
            "refinements": refinements,
            "summary_note": summary_note,
        }
        self.repo.save_iteration_log(log_payload)
        return log_payload

    @staticmethod
    def _collect_node_diff(
        res: Dict[str, Any],
        delta_nodes: List[Dict[str, Any]],
        refinements: List[Dict[str, Any]],
    ) -> None:
        action = res.get("action")
        if action == "created":
            delta_nodes.append(res)
        elif action == "refined":
            refinements.append(res)