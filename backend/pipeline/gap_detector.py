import json
from typing import Any, Dict, List

from backend.database import KGRepository, get_db_connection


class KnowledgeGapDetector:
    """
    知识缺口发现引擎：
    基于当前图谱的节点、边与属性完整性，自动识别知识缺口并生成下一轮多源检索策略与理由。
    """

    TARGET_COMPARISON_RESINS = {
        "resin:ni_sepharose": "Ni Sepharose",
        "resin:histrap": "HisTrap",
        "resin:ni_ida": "Ni-IDA",
    }

    def __init__(self, repo: KGRepository):
        self.repo = repo

    def detect_and_plan_next_round(self, current_round: int) -> Dict[str, Any]:
        """
        分析当前图谱中的缺口，写入 knowledge_gaps 表，并返回下一轮的：
        - search_rationale（检索理由）
        - queries（各数据源检索式）
        - detected_gaps（本轮发现的缺口列表）
        """
        next_round = current_round + 1
        gaps: List[Dict[str, str]] = []

        with get_db_connection(self.repo.db_path) as conn:
            condition_rows = conn.execute(
                "SELECT node_id, label, properties_json FROM nodes WHERE node_type = 'ConditionSet'"
            ).fetchall()
            resin_rows = conn.execute(
                "SELECT node_id, label FROM nodes WHERE node_type = 'ResinMaterial'"
            ).fetchall()
            edge_rows = conn.execute("SELECT * FROM edges").fetchall()

        existing_resin_ids = {r["node_id"] for r in resin_rows}
        missing_resins = [
            label
            for nid, label in self.TARGET_COMPARISON_RESINS.items()
            if nid not in existing_resin_ids
        ]

        # 缺口 1：检查实验条件（ConditionSet）是否存在洗脱咪唑浓度、洗涤浓度或变性剂缺失（null）
        incomplete_conditions = 0
        for row in condition_rows:
            props = json.loads(row["properties_json"])
            if not props.get("elution_imidazole_conc") and not props.get("wash_imidazole_conc"):
                incomplete_conditions += 1

        if incomplete_conditions > 0 and next_round == 2:
            gap_id = f"gap_r{current_round}_missing_imidazole_wash"
            desc = (
                f"当前图谱中有 {incomplete_conditions} 条实验条件记录缺少明确的洗涤/洗脱咪唑浓度或"
                f"大肠杆菌内源高组氨酸杂蛋白（如 SlyD P0A9K9）去除条件，需定向补充优化洗脱与抗杂蛋白文献及手册。"
            )
            gaps.append(
                {
                    "gap_id": gap_id,
                    "gap_type": "MISSING_CONDITION",
                    "description": desc,
                    "suggested_source": "OpenAlex",
                    "suggested_query": "6xHis tag IMAC imidazole wash SlyD contaminant purification purity",
                }
            )
            self.repo.record_knowledge_gap(
                gap_id=gap_id,
                detected_in_round=current_round,
                gap_type="MISSING_CONDITION",
                description=desc,
                suggested_source="OpenAlex",
                suggested_query="6xHis tag IMAC imidazole wash SlyD contaminant purification purity",
            )

        # 缺口 2：检查内源高组氨酸杂蛋白（如 SlyD）或变性/EDTA 边界条件是否覆盖
        has_denaturing_branch = any(
            json.loads(r["properties_json"]).get("denaturant") is not None
            for r in condition_rows
        )
        if not has_denaturing_branch and next_round >= 3:
            gap_id = f"gap_r{current_round}_denaturing_edta_boundary"
            desc = (
                "当前图谱以常规可溶性 IMAC 条件为主，缺少变性条件（Urea / Guanidine）或 EDTA/DTT 耐受边界"
                "的实验与手册证据，需补充极端条件下的纯化方法与活性/收率对比。"
            )
            gaps.append(
                {
                    "gap_id": gap_id,
                    "gap_type": "MISSING_EDGE_CASE",
                    "description": desc,
                    "suggested_source": "Cytiva",
                    "suggested_query": "HisTrap urea guanidine EDTA DTT denaturing His-tag purification",
                }
            )
            self.repo.record_knowledge_gap(
                gap_id=gap_id,
                detected_in_round=current_round,
                gap_type="MISSING_EDGE_CASE",
                description=desc,
                suggested_source="Cytiva",
                suggested_query="HisTrap urea guanidine EDTA DTT denaturing His-tag purification",
            )

        # 缺口 3：检查填料/柱材（ResinMaterial）是否缺少 Ni Sepharose、HisTrap 或 Ni-IDA 独立对比节点
        if missing_resins and next_round >= 4:
            gap_id = f"gap_r{current_round}_missing_resin_comparison"
            missing_str = "、".join(missing_resins)
            desc = (
                f"当前图谱的 ResinMaterial 节点仅包含基础螯合填料，缺少 {missing_str} 的独立实验证据，"
                f"需补充不同螯合配体与商业层析柱（Ni-NTA vs Ni-IDA vs Ni Sepharose / HisTrap）的纯化性能对比。"
            )
            resin_query = 'His-tag purification IMAC comparison "Ni-NTA" "Ni-IDA" "Ni Sepharose" "HisTrap"'
            gaps.append(
                {
                    "gap_id": gap_id,
                    "gap_type": "MISSING_RESIN_COMPARISON",
                    "description": desc,
                    "suggested_source": "OpenAlex + Cytiva",
                    "suggested_query": resin_query,
                }
            )
            self.repo.record_knowledge_gap(
                gap_id=gap_id,
                detected_in_round=current_round,
                gap_type="MISSING_RESIN_COMPARISON",
                description=desc,
                suggested_source="OpenAlex + Cytiva",
                suggested_query=resin_query,
            )

        # 组装下一轮检索式与检索理由
        if next_round == 2:
            rationale = (
                "第 1 轮已建立基础蛋白、Cytiva 手册与通用 IMAC 文献骨架，但发现部分实验缺少明确的咪唑洗涤/洗脱浓度，"
                "且 UniProt 识别出的大肠杆菌内源杂蛋白（如 SlyD）尚缺针对性纯化优化证据。"
                "因此第 2 轮定向检索 SlyD/咪唑洗涤优化文献及 HisTrap 化学耐受手册。"
            )
            queries = {
                "openalex_query": "6xHis tag purification imidazole wash SlyD purity",
                "uniprot_query": "(gene:slyd AND organism_id:83333) OR (gene:hisA AND organism_id:83333)",
                "cytiva_keywords": ["HisTrap", "imidazole", "wash", "purity"],
            }
        elif next_round == 3:
            rationale = (
                "第 2 轮已补充咪唑洗涤与内源杂蛋白关联，但图谱在变性纯化（Urea/Guanidine）及还原剂/螯合剂（DTT/EDTA）"
                "耐受边界上仍存在空白。第 3 轮定向拓展变性与耐受边界条件，验证图谱收敛度。"
            )
            queries = {
                "openalex_query": "His-tag purification urea guanidine EDTA DTT denaturing",
                "uniprot_query": "(accession:P04517) OR (accession:P10636)",
                "cytiva_keywords": ["EDTA", "DTT", "urea", "guanidine", "HisTrap"],
            }
        elif missing_resins:
            missing_str = "、".join(missing_resins)
            rationale = (
                f"第 {current_round} 轮完成后检测到填料/柱材维度仍缺少 {missing_str} 的独立节点。"
                f"因此第 {next_round} 轮定向检索 Ni-NTA、Ni-IDA、Ni Sepharose 与 HisTrap 的对比文献及 Cytiva 手册章节，"
                f"补全 USES_RESIN 原文证据链。"
            )
            queries = {
                "openalex_query": '"His-tag" ("Ni Sepharose" OR "HisTrap" OR "Ni-IDA") ("Ni-NTA" OR "IMAC") purification',
                "uniprot_query": "(accession:P42212) OR (accession:P04517)",
                "cytiva_keywords": ["Ni Sepharose", "HisTrap", "IMAC", "chelating"],
            }
        else:
            rationale = (
                f"第 {next_round} 轮延续既有高特异性查询词进行增量复核，用于检验图谱是否已收敛或存在残留长尾文献。"
            )
            queries = {
                "openalex_query": "6xHis tag purification imidazole wash SlyD purity",
                "uniprot_query": "(accession:P42212) OR (accession:P0A9K9)",
                "cytiva_keywords": ["HisTrap", "imidazole"],
            }

        return {
            "next_round": next_round,
            "search_rationale": rationale,
            "queries": queries,
            "detected_gaps": gaps,
        }