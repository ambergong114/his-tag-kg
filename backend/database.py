import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import DB_PATH

CYTIVA_DEFAULT_CATALOG_URL = "https://www.cytivalifesciences.com/en/us/support/handbooks"
CYTIVA_BASE_PDF_URL = "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf"
CYTIVA_HISTAG_HANDBOOK_PDF = "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf#page=29"

CONCRETE_CONDITION_KEYS = (
    "binding_buffer",
    "wash_imidazole_conc",
    "elution_imidazole_conc",
    "ph_value",
    "denaturant",
    "additive_tolerance",
    "temperature",
    "flow_rate",
)


def get_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_unsigned_cytiva_cdn(url: Optional[str]) -> bool:
    if not url:
        return False
    lower_url = url.lower()
    return (
        "cdn.cytivalifesciences.com/api/public/content/digi-" in lower_url
        and "digi-11495-pdf" not in lower_url
        and "context=" not in lower_url
    )


def _is_cytiva_official_url(url: Optional[str]) -> bool:
    if not url:
        return False
    lower_url = url.lower()
    is_domain = "cytivalifesciences.com" in lower_url or "cytiva.com" in lower_url
    is_misrouted_shop = "/shop/chromatography/" in lower_url
    return is_domain and not is_misrouted_shop and not _is_unsigned_cytiva_cdn(url)


def _extract_page_number_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\[Page\s+(\d+)\]", text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    m2 = re.search(r"\b(?:p\.|pp\.)\s*(\d+)", text, flags=re.IGNORECASE)
    if m2:
        return int(m2.group(1))
    return None


def _resolve_cytiva_pdf_with_page(
    base_url: str,
    evidence_quote: str = "",
    title: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    meta = meta or {}
    clean_base = base_url.split("#")[0] if base_url else CYTIVA_BASE_PDF_URL
    if "digi-11495-pdf" not in clean_base:
        clean_base = CYTIVA_BASE_PDF_URL

    page_num = _extract_page_number_from_text(evidence_quote)

    if not page_num and isinstance(meta.get("page_num"), int):
        page_num = meta["page_num"]
    if not page_num:
        page_num = _extract_page_number_from_text(title)

    if not page_num and evidence_quote:
        q_low = evidence_quote.lower()
        if "stripping" in q_low or "recharging" in q_low or "0.1 m niso4" in q_low:
            page_num = 42
        elif "urea" in q_low or "guanidine" in q_low or "inclusion bodies" in q_low or "refolding" in q_low:
            page_num = 38
        elif "slyd" in q_low or "contaminant" in q_low or "20 to 40 mm imidazole" in q_low:
            page_num = 31
        elif "ni sepharose 6 fast flow" in q_low or "histrap ff" in q_low:
            page_num = 36

    if not page_num and "#page=" in (base_url or ""):
        m_hash = re.search(r"#page=(\d+)", base_url)
        if m_hash:
            page_num = int(m_hash.group(1))

    final_page = page_num if page_num and page_num >= 29 else 29
    return f"{clean_base}#page={final_page}"


def _attach_openalex_direct_url(record: Dict[str, Any], meta: Dict[str, Any]) -> None:
    if record.get("source_type") != "OpenAlex":
        return

    oa_id = meta.get("openalex_id")
    source_id = record.get("source_id") or ""
    raw_url = record.get("url") or record.get("source_url") or ""

    if not oa_id and source_id.startswith("openalex_"):
        oa_id = source_id.replace("openalex_", "", 1)
    if not oa_id and "openalex.org/" in raw_url:
        oa_id = raw_url.rstrip("/").split("/")[-1]

    oa_url = meta.get("openalex_url") or (f"https://openalex.org/{oa_id}" if oa_id else None)
    if oa_url:
        meta["openalex_url"] = oa_url
        record["openalex_url"] = oa_url


def _normalize_cytiva_record(record: Dict[str, Any], meta: Dict[str, Any]) -> None:
    if record.get("source_type") != "Cytiva":
        return

    raw_catalog = meta.get("catalog_origin") or meta.get("publisher_landing_url")
    catalog_url = raw_catalog if _is_cytiva_official_url(raw_catalog) else CYTIVA_DEFAULT_CATALOG_URL

    raw_pdf = (
        meta.get("pdf_endpoint")
        or meta.get("oa_pdf_url")
        or record.get("url")
        or record.get("source_url")
        or ""
    )
    base_pdf = raw_pdf if _is_cytiva_official_url(raw_pdf) else CYTIVA_HISTAG_HANDBOOK_PDF
    pdf_url = _resolve_cytiva_pdf_with_page(
        base_pdf,
        evidence_quote=record.get("evidence_quote") or record.get("raw_text") or "",
        title=record.get("source_title") or record.get("title") or "",
        meta=meta,
    )

    if "url" in record:
        record["url"] = pdf_url
    if "source_url" in record:
        record["source_url"] = pdf_url

    record["channel_url"] = catalog_url
    record["catalog_url"] = catalog_url
    record["pdf_endpoint"] = pdf_url
    record["publisher_url"] = pdf_url

    meta["catalog_origin"] = catalog_url
    meta["publisher_landing_url"] = catalog_url
    meta["pdf_endpoint"] = pdf_url
    meta["oa_pdf_url"] = pdf_url


def _has_concrete_condition_dict(props: Dict[str, Any]) -> bool:
    return any(props.get(k) is not None and str(props.get(k)).strip() != "" for k in CONCRETE_CONDITION_KEYS)


def _format_condition_label(props: Dict[str, Any], fallback_label: str) -> str:
    """根据非空条件参数动态生成清晰可读的 ConditionSet 节点标签，消除 Wash:null 占位符"""
    parts: List[str] = []
    if props.get("wash_imidazole_conc"):
        parts.append(f"Wash: {props['wash_imidazole_conc']}")
    if props.get("elution_imidazole_conc"):
        parts.append(f"Elute: {props['elution_imidazole_conc']}")
    if props.get("ph_value"):
        parts.append(str(props["ph_value"]))
    if props.get("binding_buffer"):
        parts.append(str(props["binding_buffer"]))
    if props.get("denaturant"):
        parts.append(f"Denat: {props['denaturant']}")
    if props.get("additive_tolerance"):
        parts.append(str(props["additive_tolerance"]))
    if props.get("temperature"):
        parts.append(str(props["temperature"]))
    if props.get("flow_rate"):
        parts.append(str(props["flow_rate"]))

    if parts:
        return "Cond (" + " | ".join(parts[:3]) + ")"
    return fallback_label


def hydrate_condition_properties(
    raw_props: Dict[str, Any],
    evidence_quote: str = "",
    node_label: str = "",
) -> Dict[str, Any]:
    """
    严格基于原文引证（evidence_quote）和节点标签提取明确写出的纯化条件参数；
    原文未提及的数值字段显式保持为 None（严禁补猜）。
    """
    schema_keys = [
        "condition_summary",
        *CONCRETE_CONDITION_KEYS,
    ]
    props: Dict[str, Any] = {k: raw_props.get(k) for k in schema_keys}
    for k, v in raw_props.items():
        if k not in props and k != "has_concrete_condition":
            props[k] = v

    text = f"{node_label} {evidence_quote}".strip()
    if not text:
        return props

    clean_text = re.sub(r"\[Page\s+\d+\]\s*", "", (evidence_quote or node_label)).strip()

    if not props.get("condition_summary") and clean_text:
        props["condition_summary"] = clean_text[:240]

    if not props.get("binding_buffer"):
        buf_parts: List[str] = []
        m_buf = re.search(
            r"(\d+(?:\.\d+)?\s*mM\s+(?:sodium phosphate|potassium phosphate|Tris(?:-HCl)?|HEPES|PBS|phosphate buffer))",
            text,
            flags=re.IGNORECASE,
        )
        if m_buf:
            buf_parts.append(m_buf.group(1).strip())
        m_nacl = re.search(r"(\d+(?:\.\d+)?\s*(?:M|mM)\s*NaCl)", text, flags=re.IGNORECASE)
        if m_nacl:
            buf_parts.append(m_nacl.group(1).strip())
        if buf_parts:
            props["binding_buffer"] = ", ".join(buf_parts)

    if not props.get("wash_imidazole_conc"):
        m_wash = re.search(
            r"(?:binding|wash(?:ing)?|sample|equilibrat\w*)[^.;]{0,55}?(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole",
            text,
            flags=re.IGNORECASE,
        ) or re.search(
            r"(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole[^.;]{0,45}?(?:binding|wash|minimize|equilibrat)",
            text,
            flags=re.IGNORECASE,
        )
        if m_wash:
            props["wash_imidazole_conc"] = f"{m_wash.group(1).strip()} imidazole"

    if not props.get("elution_imidazole_conc"):
        m_elute = re.search(
            r"(?:elution|elute[sd]?|step|gradient)[^.;]{0,55}?(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole",
            text,
            flags=re.IGNORECASE,
        ) or re.search(
            r"(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole[^.;]{0,45}?(?:elution|elute|gradient)",
            text,
            flags=re.IGNORECASE,
        )
        if m_elute:
            props["elution_imidazole_conc"] = f"{m_elute.group(1).strip()} imidazole"

    if not props.get("wash_imidazole_conc") and not props.get("elution_imidazole_conc"):
        m_any = re.search(r"(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole", text, flags=re.IGNORECASE)
        if m_any:
            val = f"{m_any.group(1).strip()} imidazole"
            if "wash" in text.lower() or "binding" in text.lower():
                props["wash_imidazole_conc"] = val
            else:
                props["elution_imidazole_conc"] = val

    if not props.get("ph_value"):
        m_ph = re.search(r"\bp[hH]\s*[:=]?\s*(\d+(?:\.\d+)?(?:\s*(?:to|-|–)\s*\d+(?:\.\d+)?)?)", text)
        if m_ph:
            props["ph_value"] = f"pH {m_ph.group(1).strip()}"

    if not props.get("denaturant"):
        m_denat = re.findall(
            r"(\d+(?:\.\d+)?\s*M\s*(?:urea|guanidine(?:\s+hydrochloride|-HCl)?))",
            text,
            flags=re.IGNORECASE,
        )
        if m_denat:
            props["denaturant"] = ", ".join(dict.fromkeys(m.strip() for m in m_denat))

    if not props.get("additive_tolerance"):
        additives: List[str] = []
        for pattern in [
            r"((?:up to\s*)?\d+(?:\.\d+)?\s*mM\s*EDTA)",
            r"((?:up to\s*)?\d+(?:\.\d+)?\s*mM\s*(?:DTT|TCEP|β-mercaptoethanol|2-mercaptoethanol))",
            r"(\d+(?:\.\d+)?\s*M\s*NiSO4)",
        ]:
            for match in re.findall(pattern, text, flags=re.IGNORECASE):
                if match.strip() not in additives:
                    additives.append(match.strip())
        if additives:
            props["additive_tolerance"] = ", ".join(additives)

    if not props.get("temperature"):
        m_temp = re.search(r"(\d+(?:\.\d+)?\s*°?\s*C|room temperature)", text, flags=re.IGNORECASE)
        if m_temp:
            props["temperature"] = m_temp.group(1).strip()

    if not props.get("flow_rate"):
        m_flow = re.search(r"(\d+(?:\.\d+)?\s*(?:mL/min|cm/h))", text, flags=re.IGNORECASE)
        if m_flow:
            props["flow_rate"] = m_flow.group(1).strip()

    return props


@contextmanager
def get_db_connection(db_path: Path = DB_PATH):
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(db_path: Path = DB_PATH) -> None:
    schema_sql = """
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        raw_text TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        content_hash TEXT NOT NULL UNIQUE,
        discovered_round INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS nodes (
        node_id TEXT PRIMARY KEY,
        node_type TEXT NOT NULL,
        label TEXT NOT NULL,
        properties_json TEXT NOT NULL DEFAULT '{}',
        primary_source_type TEXT NOT NULL,
        discovered_round INTEGER NOT NULL,
        updated_round INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS edges (
        edge_id TEXT PRIMARY KEY,
        source_node TEXT NOT NULL,
        target_node TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        properties_json TEXT NOT NULL DEFAULT '{}',
        source_id TEXT NOT NULL,
        evidence_quote TEXT NOT NULL,
        discovered_round INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (source_node) REFERENCES nodes(node_id) ON DELETE CASCADE,
        FOREIGN KEY (target_node) REFERENCES nodes(node_id) ON DELETE CASCADE,
        FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS iteration_logs (
        round_num INTEGER PRIMARY KEY,
        status TEXT NOT NULL,
        search_rationale TEXT NOT NULL,
        queries_json TEXT NOT NULL DEFAULT '{}',
        new_sources_count INTEGER NOT NULL DEFAULT 0,
        new_nodes_count INTEGER NOT NULL DEFAULT 0,
        new_edges_count INTEGER NOT NULL DEFAULT 0,
        refinements_count INTEGER NOT NULL DEFAULT 0,
        delta_nodes_json TEXT NOT NULL DEFAULT '[]',
        delta_edges_json TEXT NOT NULL DEFAULT '[]',
        refinements_json TEXT NOT NULL DEFAULT '[]',
        summary_note TEXT NOT NULL,
        executed_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS knowledge_gaps (
        gap_id TEXT PRIMARY KEY,
        detected_in_round INTEGER NOT NULL,
        gap_type TEXT NOT NULL,
        description TEXT NOT NULL,
        suggested_source TEXT NOT NULL,
        suggested_query TEXT NOT NULL,
        resolved_in_round INTEGER,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
    CREATE INDEX IF NOT EXISTS idx_nodes_round ON nodes(discovered_round);
    CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_node);
    CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_node);
    CREATE INDEX IF NOT EXISTS idx_edges_round ON edges(discovered_round);
    """
    with get_db_connection(db_path) as conn:
        conn.executescript(schema_sql)


class KGRepository:
    """知识图谱数据访问层（DAO）：封装去重写入、属性补全追踪与图谱查询"""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        init_database(self.db_path)
        self._sanitize_cytiva_urls()
        self._backfill_condition_nodes()

    def _sanitize_cytiva_urls(self) -> None:
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                DELETE FROM sources
                WHERE source_type = 'Cytiva'
                  AND (
                    (LOWER(url) NOT LIKE '%cytivalifesciences.com%' AND LOWER(url) NOT LIKE '%cytiva.com%')
                    OR LOWER(metadata_json) LIKE '%europepmc%'
                  )
                """
            )
            conn.execute(
                """
                UPDATE sources
                SET url = ?
                WHERE source_type = 'Cytiva'
                  AND (
                    LOWER(url) LIKE '%/shop/chromatography/%'
                    OR url = ?
                    OR url LIKE '%#page=26%'
                    OR (
                        LOWER(url) LIKE '%cdn.cytivalifesciences.com/api/public/content/digi-%'
                        AND LOWER(url) NOT LIKE '%digi-11495-pdf%'
                        AND LOWER(url) NOT LIKE '%context=%'
                    )
                  )
                """,
                (CYTIVA_HISTAG_HANDBOOK_PDF, CYTIVA_DEFAULT_CATALOG_URL),
            )

    def _backfill_condition_nodes(self) -> None:
        """启动时补全历史 ConditionSet 节点的参数与可读标题，并移除无任何具体参数的空占位节点"""
        with get_db_connection(self.db_path) as conn:
            cond_rows = conn.execute(
                "SELECT node_id, label, properties_json FROM nodes WHERE node_type = 'ConditionSet'"
            ).fetchall()
            for row in cond_rows:
                node_id = row["node_id"]
                old_props = json.loads(row["properties_json"])
                edge_rows = conn.execute(
                    "SELECT evidence_quote, properties_json FROM edges WHERE target_node = ? OR source_node = ?",
                    (node_id, node_id),
                ).fetchall()
                combined_quotes = " ".join(e["evidence_quote"] for e in edge_rows if e["evidence_quote"])
                merged_edge_props: Dict[str, Any] = {}
                for e in edge_rows:
                    merged_edge_props.update(json.loads(e["properties_json"]))

                enriched = hydrate_condition_properties(
                    {**old_props, **merged_edge_props},
                    evidence_quote=combined_quotes,
                    node_label=row["label"],
                )
                for k, v in enriched.items():
                    if v is not None and v != "" and old_props.get(k) in (None, ""):
                        old_props[k] = v

                if not _has_concrete_condition_dict(old_props):
                    conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
                else:
                    new_label = _format_condition_label(old_props, row["label"])
                    conn.execute(
                        "UPDATE nodes SET label = ?, properties_json = ? WHERE node_id = ?",
                        (new_label, json.dumps(old_props, ensure_ascii=False), node_id),
                    )

    def reset_database(self) -> None:
        with get_db_connection(self.db_path) as conn:
            conn.executescript("""
                DELETE FROM edges;
                DELETE FROM nodes;
                DELETE FROM sources;
                DELETE FROM iteration_logs;
                DELETE FROM knowledge_gaps;
            """)

    def source_exists_by_hash(self, content_hash: str) -> bool:
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sources WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            return row is not None

    def insert_source(
        self,
        source_id: str,
        source_type: str,
        title: str,
        url: str,
        raw_text: str,
        metadata: Dict[str, Any],
        content_hash: str,
        discovered_round: int,
    ) -> bool:
        if source_type == "Cytiva" and not _is_cytiva_official_url(url):
            raise ValueError(f"非法 Cytiva 来源链接（必须为稳定的 Cytiva 官网页面或有效 PDF）: {url}")

        if self.source_exists_by_hash(content_hash):
            return False
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sources
                (source_id, source_type, title, url, raw_text, metadata_json, content_hash, discovered_round, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    source_type,
                    title,
                    url,
                    raw_text,
                    json.dumps(metadata, ensure_ascii=False),
                    content_hash,
                    discovered_round,
                    get_utc_now(),
                ),
            )
            return conn.total_changes > 0

    def get_all_sources(self) -> List[Dict[str, Any]]:
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM sources ORDER BY discovered_round ASC, created_at ASC"
            ).fetchall()
            result = []
            for r in rows:
                item = dict(r)
                meta = json.loads(item.pop("metadata_json"))
                _attach_openalex_direct_url(item, meta)
                _normalize_cytiva_record(item, meta)
                item["metadata"] = meta
                result.append(item)
            return result

    def upsert_node(
        self,
        node_id: str,
        node_type: str,
        label: str,
        properties: Dict[str, Any],
        primary_source_type: str,
        round_num: int,
    ) -> Dict[str, Any]:
        now = get_utc_now()
        if node_type == "ConditionSet":
            clean_props = hydrate_condition_properties(properties, node_label=label)
            # 没有任何具体实验数值/化学参数时，拒绝创建全 null 的 ConditionSet 节点
            if not _has_concrete_condition_dict(clean_props):
                return {"action": "skipped_empty_condition", "node_id": node_id}
            effective_label = _format_condition_label(clean_props, label)
        else:
            clean_props = dict(properties)
            effective_label = label

        with get_db_connection(self.db_path) as conn:
            existing = conn.execute(
                "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO nodes
                    (node_id, node_type, label, properties_json, primary_source_type, discovered_round, updated_round, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        node_type,
                        effective_label,
                        json.dumps(clean_props, ensure_ascii=False),
                        primary_source_type,
                        round_num,
                        round_num,
                        now,
                    ),
                )
                return {
                    "action": "created",
                    "node_id": node_id,
                    "label": effective_label,
                    "node_type": node_type,
                }

            old_props = json.loads(existing["properties_json"])
            old_source = existing["primary_source_type"]
            changes = []

            for k, v in clean_props.items():
                if v is not None and v != "" and (k not in old_props or old_props[k] in (None, "")):
                    old_props[k] = v
                    changes.append(f"补全属性 {k}: {v}")

            new_source = old_source
            if old_source != primary_source_type and old_source != "Multi-Source":
                new_source = "Multi-Source"
                changes.append(f"跨源实体对齐融合 ({old_source} + {primary_source_type})")

            updated_label = (
                _format_condition_label(old_props, existing["label"])
                if node_type == "ConditionSet"
                else existing["label"]
            )

            if changes or updated_label != existing["label"]:
                conn.execute(
                    """
                    UPDATE nodes
                    SET label = ?, properties_json = ?, primary_source_type = ?, updated_round = ?
                    WHERE node_id = ?
                    """,
                    (
                        updated_label,
                        json.dumps(old_props, ensure_ascii=False),
                        new_source,
                        round_num,
                        node_id,
                    ),
                )
                return {
                    "action": "refined",
                    "node_id": node_id,
                    "label": updated_label,
                    "details": changes,
                }

            return {"action": "unchanged", "node_id": node_id}

    def insert_edge(
        self,
        edge_id: str,
        source_node: str,
        target_node: str,
        relation_type: str,
        source_id: str,
        evidence_quote: str,
        round_num: int,
        properties: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not evidence_quote or not evidence_quote.strip():
            raise ValueError(f"Edge {edge_id} 缺少原文证据 (evidence_quote)，拒绝写入图谱！")

        props = properties or {}
        with get_db_connection(self.db_path) as conn:
            # 若端点之一是因无数值而被跳过的空 ConditionSet，安全跳过该边，避免外键异常
            src_exists = conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (source_node,)).fetchone()
            tgt_exists = conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (target_node,)).fetchone()
            if not src_exists or not tgt_exists:
                return False

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO edges
                (edge_id, source_node, target_node, relation_type, properties_json, source_id, evidence_quote, discovered_round, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge_id,
                    source_node,
                    target_node,
                    relation_type,
                    json.dumps(props, ensure_ascii=False),
                    source_id,
                    evidence_quote.strip(),
                    round_num,
                    get_utc_now(),
                ),
            )
            inserted = cursor.rowcount > 0

            for candidate_node_id in (target_node, source_node):
                node_row = conn.execute(
                    "SELECT node_type, label, properties_json FROM nodes WHERE node_id = ?",
                    (candidate_node_id,),
                ).fetchone()
                if node_row and node_row["node_type"] == "ConditionSet":
                    old_props = json.loads(node_row["properties_json"])
                    enriched = hydrate_condition_properties(
                        {**old_props, **props},
                        evidence_quote=evidence_quote,
                        node_label=node_row["label"],
                    )
                    updated = False
                    for k, v in enriched.items():
                        if v is not None and v != "" and old_props.get(k) in (None, ""):
                            old_props[k] = v
                            updated = True
                    if updated:
                        new_label = _format_condition_label(old_props, node_row["label"])
                        conn.execute(
                            "UPDATE nodes SET label = ?, properties_json = ?, updated_round = ? WHERE node_id = ?",
                            (new_label, json.dumps(old_props, ensure_ascii=False), round_num, candidate_node_id),
                        )

            return inserted

    def get_full_graph(
        self,
        protein_filter: Optional[str] = None,
        method_filter: Optional[str] = None,
        max_round: Optional[int] = None,
    ) -> Dict[str, Any]:
        with get_db_connection(self.db_path) as conn:
            node_query = "SELECT * FROM nodes WHERE 1=1"
            params: List[Any] = []
            if max_round is not None:
                node_query += " AND discovered_round <= ?"
                params.append(max_round)

            nodes_rows = conn.execute(node_query, params).fetchall()
            nodes = []
            for r in nodes_rows:
                n = dict(r)
                n["properties"] = json.loads(n.pop("properties_json"))
                nodes.append(n)

            edge_query = """
                SELECT e.*, s.title AS source_title, s.url AS source_url, s.source_type, s.metadata_json
                FROM edges e
                JOIN sources s ON e.source_id = s.source_id
                WHERE 1=1
            """
            e_params: List[Any] = []
            if max_round is not None:
                edge_query += " AND e.discovered_round <= ?"
                e_params.append(max_round)

            edges_rows = conn.execute(edge_query, e_params).fetchall()
            edges = []
            for r in edges_rows:
                e = dict(r)
                e["properties"] = json.loads(e.pop("properties_json"))
                meta = json.loads(e.pop("metadata_json"))
                _attach_openalex_direct_url(e, meta)
                _normalize_cytiva_record(e, meta)
                e["metadata"] = meta
                edges.append(e)

            if protein_filter or method_filter:
                node_map = {n["node_id"]: n for n in nodes}
                valid_runs = {n["node_id"] for n in nodes if n["node_type"] == "ExperimentRun"}

                matched_proteins = set()
                if protein_filter:
                    pf_lower = protein_filter.strip().lower()
                    for n in nodes:
                        if n["node_type"] == "Protein" and (
                            n["node_id"].lower() == pf_lower
                            or pf_lower in n["label"].lower()
                            or pf_lower == str(n["properties"].get("uniprot_accession", "")).lower()
                        ):
                            matched_proteins.add(n["node_id"])

                    runs_for_protein = {
                        e["source_node"]
                        for e in edges
                        if e["relation_type"] in ("PURIFIES", "TARGETS_CONTAMINANT")
                        and e["target_node"] in matched_proteins
                    }
                    valid_runs &= runs_for_protein

                matched_methods = set()
                if method_filter:
                    mf_lower = method_filter.strip().lower()
                    for n in nodes:
                        if n["node_type"] == "PurificationMethod" and (
                            n["node_id"].lower() == mf_lower
                            or mf_lower in n["label"].lower()
                        ):
                            matched_methods.add(n["node_id"])

                    runs_for_method = {
                        e["source_node"]
                        for e in edges
                        if e["relation_type"] == "USES_METHOD"
                        and e["target_node"] in matched_methods
                    }
                    valid_runs &= runs_for_method

                kept_nodes = set(valid_runs) | matched_proteins | matched_methods
                kept_edges = []
                for e in edges:
                    src, tgt = e["source_node"], e["target_node"]
                    if src not in valid_runs:
                        continue
                    target_node = node_map.get(tgt)
                    if (
                        protein_filter
                        and target_node
                        and target_node["node_type"] == "Protein"
                        and tgt not in matched_proteins
                    ):
                        continue
                    kept_edges.append(e)
                    kept_nodes.add(tgt)

                nodes = [n for n in nodes if n["node_id"] in kept_nodes]
                edges = kept_edges

            return {
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "total_nodes": len(nodes),
                    "total_edges": len(edges),
                },
            }


    def get_edge_evidence(self, edge_id: str) -> Optional[Dict[str, Any]]:
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT e.*, s.title AS source_title, s.url AS source_url,
                       s.source_type, s.raw_text AS full_source_text, s.metadata_json
                FROM edges e
                JOIN sources s ON e.source_id = s.source_id
                WHERE e.edge_id = ?
                """,
                (edge_id,),
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            data["properties"] = json.loads(data.pop("properties_json"))
            meta = json.loads(data.pop("metadata_json"))
            _attach_openalex_direct_url(data, meta)
            _normalize_cytiva_record(data, meta)
            data["source_metadata"] = meta
            data["metadata"] = meta
            return data

    def save_iteration_log(self, log_data: Dict[str, Any]) -> None:
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO iteration_logs
                (round_num, status, search_rationale, queries_json,
                 new_sources_count, new_nodes_count, new_edges_count, refinements_count,
                 delta_nodes_json, delta_edges_json, refinements_json, summary_note, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_data["round_num"],
                    log_data["status"],
                    log_data["search_rationale"],
                    json.dumps(log_data.get("queries", {}), ensure_ascii=False),
                    log_data.get("new_sources_count", 0),
                    log_data.get("new_nodes_count", 0),
                    log_data.get("new_edges_count", 0),
                    log_data.get("refinements_count", 0),
                    json.dumps(log_data.get("delta_nodes", []), ensure_ascii=False),
                    json.dumps(log_data.get("delta_edges", []), ensure_ascii=False),
                    json.dumps(log_data.get("refinements", []), ensure_ascii=False),
                    log_data["summary_note"],
                    get_utc_now(),
                ),
            )

    def get_iteration_logs(self) -> List[Dict[str, Any]]:
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM iteration_logs ORDER BY round_num ASC"
            ).fetchall()
            logs = []
            for r in rows:
                item = dict(r)
                item["queries"] = json.loads(item.pop("queries_json"))
                item["delta_nodes"] = json.loads(item.pop("delta_nodes_json"))
                item["delta_edges"] = json.loads(item.pop("delta_edges_json"))
                item["refinements"] = json.loads(item.pop("refinements_json"))
                logs.append(item)
            return logs

    def record_knowledge_gap(
        self,
        gap_id: str,
        detected_in_round: int,
        gap_type: str,
        description: str,
        suggested_source: str,
        suggested_query: str,
    ) -> None:
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO knowledge_gaps
                (gap_id, detected_in_round, gap_type, description, suggested_source, suggested_query, resolved_in_round, created_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    gap_id,
                    detected_in_round,
                    gap_type,
                    description,
                    suggested_source,
                    suggested_query,
                    get_utc_now(),
                ),
            )

    def resolve_gap(self, gap_id: str, resolved_in_round: int) -> None:
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE knowledge_gaps SET resolved_in_round = ? WHERE gap_id = ?",
                (resolved_in_round, gap_id),
            )

    def get_unresolved_gaps(self) -> List[Dict[str, Any]]:
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_gaps WHERE resolved_in_round IS NULL ORDER BY detected_in_round ASC"
            ).fetchall()
            return [dict(r) for r in rows]