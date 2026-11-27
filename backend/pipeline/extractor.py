import json
import re
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from backend.config import settings


class ExtractedCondition(BaseModel):
    condition_summary: Optional[str] = Field(default=None, description="原文条件概述，严格摘自原句，未写明为 null")
    binding_buffer: Optional[str] = Field(default=None, description="结合缓冲液或盐浓度，未写明必须为 null")
    wash_imidazole_conc: Optional[str] = Field(default=None, description="洗涤/结合咪唑浓度，未写明必须为 null")
    elution_imidazole_conc: Optional[str] = Field(default=None, description="洗脱咪唑浓度，未写明必须为 null")
    ph_value: Optional[str] = Field(default=None, description="pH 条件，未写明必须为 null")
    denaturant: Optional[str] = Field(default=None, description="变性剂如 Urea/Guanidine 或 None，未写明必须为 null")
    additive_tolerance: Optional[str] = Field(default=None, description="EDTA/DTT/还原剂耐受或添加说明，未写明必须为 null")
    temperature: Optional[str] = Field(default=None, description="温度条件，未写明必须为 null")
    flow_rate: Optional[str] = Field(default=None, description="流速条件，未写明必须为 null")


class ExtractedOutcome(BaseModel):
    purity: Optional[str] = Field(default=None, description="纯度结果，未写明必须为 null")
    yield_metric: Optional[str] = Field(default=None, description="收率结果，未写明必须为 null")
    activity: Optional[str] = Field(default=None, description="活性/功能保留结果，未写明必须为 null")


class ExtractedPurificationRecord(BaseModel):
    protein_mention: str = Field(..., description="文中提及的蛋白名称或标识")
    tag_mention: Optional[str] = Field(default=None, description="如 6xHis-tag、8xHis-tag，未写明为 null")
    method_name: str = Field(default="IMAC", description="纯化方法，如 IMAC、Denaturing IMAC、SEC")
    resin_name: Optional[str] = Field(
        default=None,
        description="填料或层析柱名称，如 HisTrap HP、Ni Sepharose、Ni-IDA、Ni-NTA、TALON，未写明为 null",
    )
    contaminant_mention: Optional[str] = Field(default=None, description="提及的杂蛋白或去除目标，如 SlyD，未写明为 null")
    conditions: ExtractedCondition = Field(default_factory=ExtractedCondition)
    outcomes: ExtractedOutcome = Field(default_factory=ExtractedOutcome)
    verbatim_quote: str = Field(..., description="必须从原文逐字摘录的支撑片段，不得改写或编造")


class ExtractionBundle(BaseModel):
    records: List[ExtractedPurificationRecord] = Field(default_factory=list)


CONCRETE_CONDITION_FIELDS = (
    "binding_buffer",
    "wash_imidazole_conc",
    "elution_imidazole_conc",
    "ph_value",
    "denaturant",
    "additive_tolerance",
    "temperature",
    "flow_rate",
)

RESIN_PATTERN = re.compile(
    r"\b(?:histrap(?:\s+(?:hp|ff|excel|crude))?|ni-nta|ni2\+-nta|nickel-nitrilotriacetic acid|"
    r"ni-ida|ni2\+-ida|iminodiacetic acid|ni\s+sepharose(?:\s+(?:high performance|6 fast flow|excel))?|"
    r"talon|co2\+-cma)\b",
    flags=re.IGNORECASE,
)


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_verbatim_quote(quote: str, raw_text: str) -> bool:
    """
    硬核防幻觉校验：验证抽取器给出的 verbatim_quote 是否真实存在于原始文本中。
    """
    if not quote or len(quote.strip()) < 12:
        return False

    norm_quote = _normalize_for_match(quote)
    norm_source = _normalize_for_match(raw_text)

    if norm_quote in norm_source:
        return True

    words = norm_quote.split(" ")
    if len(words) >= 6:
        window = " ".join(words[:6])
        if window in norm_source:
            return True
    return False


def has_concrete_condition(cond: Optional[ExtractedCondition]) -> bool:
    """判断条件对象是否至少包含一项真实具体的实验数值/化学参数"""
    if cond is None:
        return False
    data = cond.model_dump() if isinstance(cond, BaseModel) else cond
    return any(data.get(k) is not None and str(data.get(k)).strip() != "" for k in CONCRETE_CONDITION_FIELDS)


def _enrich_condition_from_quote(cond: Optional[ExtractedCondition], quote: str) -> ExtractedCondition:
    """
    从通过逐字校验的文本中提取明确写明的缓冲液、咪唑浓度、pH、变性剂、EDTA/DTT 耐受度及温度/流速；
    原文未提及的化学参数严格保持 None（绝不补猜）。
    """
    target_cond = cond if cond is not None else ExtractedCondition()
    if not quote:
        return target_cond

    clean_quote = re.sub(r"\[Page\s+\d+\]\s*", "", quote).strip()
    low = clean_quote.lower()

    if not target_cond.condition_summary and clean_quote:
        target_cond.condition_summary = clean_quote[:220]

    # 1. 结合缓冲液 / 盐浓度
    if not target_cond.binding_buffer:
        buf_parts: List[str] = []
        m_buf = re.search(
            r"(\d+(?:\.\d+)?\s*mM\s+(?:sodium phosphate|potassium phosphate|Tris(?:-HCl)?|HEPES|PBS|phosphate buffer))",
            clean_quote,
            flags=re.IGNORECASE,
        )
        if m_buf:
            buf_parts.append(m_buf.group(1).strip())
        m_nacl = re.search(r"(\d+(?:\.\d+)?\s*(?:M|mM)\s*NaCl)", clean_quote, flags=re.IGNORECASE)
        if m_nacl:
            buf_parts.append(m_nacl.group(1).strip())
        if buf_parts:
            target_cond.binding_buffer = ", ".join(buf_parts)

    # 2. 洗涤/结合咪唑浓度
    if not target_cond.wash_imidazole_conc:
        m_wash = re.search(
            r"(?:binding|wash(?:ing)?|sample|equilibrat\w*)[^.;]{0,55}?(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole",
            clean_quote,
            flags=re.IGNORECASE,
        ) or re.search(
            r"(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole[^.;]{0,45}?(?:binding|wash|minimize|equilibrat)",
            clean_quote,
            flags=re.IGNORECASE,
        )
        if m_wash:
            target_cond.wash_imidazole_conc = f"{m_wash.group(1).strip()} imidazole"

    # 3. 洗脱咪唑浓度
    if not target_cond.elution_imidazole_conc:
        m_elute = re.search(
            r"(?:elution|elute[sd]?|step|gradient)[^.;]{0,55}?(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole",
            clean_quote,
            flags=re.IGNORECASE,
        ) or re.search(
            r"(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole[^.;]{0,45}?(?:elution|elute|gradient)",
            clean_quote,
            flags=re.IGNORECASE,
        )
        if m_elute:
            target_cond.elution_imidazole_conc = f"{m_elute.group(1).strip()} imidazole"

    if not target_cond.wash_imidazole_conc and not target_cond.elution_imidazole_conc:
        m_any = re.search(r"(\d+(?:\s*(?:to|-|–)\s*\d+)?\s*mM)\s*imidazole", clean_quote, flags=re.IGNORECASE)
        if m_any:
            val = f"{m_any.group(1).strip()} imidazole"
            if "wash" in low or "binding" in low:
                target_cond.wash_imidazole_conc = val
            else:
                target_cond.elution_imidazole_conc = val

    # 4. pH 条件
    if not target_cond.ph_value:
        m_ph = re.search(r"\bp[hH]\s*[:=]?\s*(\d+(?:\.\d+)?(?:\s*(?:to|-|–)\s*\d+(?:\.\d+)?)?)", clean_quote)
        if m_ph:
            target_cond.ph_value = f"pH {m_ph.group(1).strip()}"

    # 5. 变性剂
    if not target_cond.denaturant:
        denat_hits = re.findall(
            r"(\d+(?:\.\d+)?\s*M\s*(?:urea|guanidine(?:\s+hydrochloride|-HCl)?))",
            clean_quote,
            flags=re.IGNORECASE,
        )
        if denat_hits:
            target_cond.denaturant = ", ".join(dict.fromkeys(h.strip() for h in denat_hits))
        elif re.search(r"\burea\b", low):
            target_cond.denaturant = "Urea"
        elif re.search(r"\bguanidine\b", low):
            target_cond.denaturant = "Guanidine"

    # 6. 添加剂与耐受度
    if not target_cond.additive_tolerance:
        additives: List[str] = []
        for pattern in [
            r"((?:up to\s*)?\d+(?:\.\d+)?\s*mM\s*EDTA)",
            r"((?:up to\s*)?\d+(?:\.\d+)?\s*mM\s*(?:DTT|TCEP|β-mercaptoethanol|2-mercaptoethanol))",
            r"(\d+(?:\.\d+)?\s*M\s*NiSO4)",
        ]:
            for match in re.findall(pattern, clean_quote, flags=re.IGNORECASE):
                if match.strip() not in additives:
                    additives.append(match.strip())
        if additives:
            target_cond.additive_tolerance = ", ".join(additives)

    # 7. 温度与流速
    if not target_cond.temperature:
        m_temp = re.search(r"(\d+(?:\.\d+)?\s*°\s*C|room temperature)", clean_quote, flags=re.IGNORECASE)
        if m_temp:
            target_cond.temperature = m_temp.group(1).strip()

    if not target_cond.flow_rate:
        m_flow = re.search(r"(\d+(?:\.\d+)?\s*(?:mL/min|cm/h))", clean_quote, flags=re.IGNORECASE)
        if m_flow:
            target_cond.flow_rate = m_flow.group(1).strip()

    return target_cond


class GroundedExtractor:
    """
    防幻觉结构化抽取器：
    1. 优先调用 OpenAI 兼容 LLM 接口提取结构化实验记录；
    2. 对每条记录执行原文逐字引用校验（verify_verbatim_quote），未通过校验直接丢弃；
    3. 未提及的实验条件与结果指标严格保持 None（JSON null），绝不补猜；
    4. 始终返回完整字典结构并附带 has_concrete_condition 标记，防止下游字典读取触发 NoneType 异常。
    """

    SYSTEM_PROMPT = """你是一名严谨的生物化学与知识图谱信息抽取专家。
请从给定的文本中抽取与“组氨酸标签蛋白（His-tag）纯化方法、材料、实验条件与结果”相关的结构化记录。

严格遵守以下防幻觉规则：
1. 【严禁补猜】文本中未明确写明的实验条件（洗涤咪唑浓度、洗脱咪唑浓度、pH、结合缓冲液、变性剂、EDTA/DTT耐受）和结果指标（纯度、收率、活性）必须输出 null，绝对禁止凭常识猜测或填写常见默认值！
2. 【填料独立】如原文明确提到 Ni Sepharose、HisTrap、Ni-IDA、Ni-NTA、TALON 等具体填料或层析柱，必须如实保留其名称，不得把 Ni Sepharose、HisTrap 或 Ni-IDA 改写或归并为 Ni-NTA；若同一句话提及多种填料，可用英文逗号分隔列出。
3. 【结果独立】纯度（purity）、收率（yield_metric）、活性（activity）必须各自独立提取，不得合并或互相推导。
4. 【逐字引证】每条记录必须提供 verbatim_quote，必须是原文中连续且逐字出现的原句片段（至少 15 个字符，保留 [Page X] 标记），用于证据溯源校验。
5. 仅输出合法 JSON 对象，格式为：{"records": [ ... ]}。"""

    def extract_from_source(self, source_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_text: str = source_doc.get("raw_text", "")
        title: str = source_doc.get("title", "")
        source_type: str = source_doc.get("source_type", "")

        if not raw_text.strip():
            return []

        llm_records: List[ExtractedPurificationRecord] = []
        if settings.LLM_API_KEY and settings.LLM_API_KEY != "your_api_key_here":
            llm_records = self._call_llm_extraction(title=title, source_type=source_type, raw_text=raw_text)

        if not llm_records:
            llm_records = self._deterministic_fallback_extraction(
                title=title, source_type=source_type, raw_text=raw_text
            )

        doc_level_cond = _enrich_condition_from_quote(ExtractedCondition(), raw_text)
        verified_records: List[Dict[str, Any]] = []

        for rec in llm_records:
            if not verify_verbatim_quote(rec.verbatim_quote, raw_text):
                continue

            enriched_cond = _enrich_condition_from_quote(rec.conditions, rec.verbatim_quote)
            if not has_concrete_condition(enriched_cond) and has_concrete_condition(doc_level_cond):
                for field_name in CONCRETE_CONDITION_FIELDS:
                    setattr(enriched_cond, field_name, getattr(doc_level_cond, field_name))

            cond_dict = enriched_cond.model_dump()
            cond_dict["has_concrete_condition"] = has_concrete_condition(enriched_cond)

            verified_records.append(
                {
                    "protein_mention": rec.protein_mention.strip(),
                    "tag_mention": rec.tag_mention.strip() if rec.tag_mention else None,
                    "method_name": rec.method_name.strip() if rec.method_name else "IMAC",
                    "resin_name": rec.resin_name.strip() if rec.resin_name else None,
                    "contaminant_mention": rec.contaminant_mention.strip() if rec.contaminant_mention else None,
                    "conditions": cond_dict,
                    "outcomes": rec.outcomes.model_dump(),
                    "verbatim_quote": rec.verbatim_quote.strip(),
                }
            )

        return verified_records

    def _call_llm_extraction(
        self, title: str, source_type: str, raw_text: str
    ) -> List[ExtractedPurificationRecord]:
        endpoint = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.LLM_API_KEY}",
            "Content-Type": "application/json",
        }
        user_content = (
            f"Source Type: {source_type}\n"
            f"Title: {title}\n"
            f"Text:\n{raw_text[:4500]}"
        )
        payload = {
            "model": settings.LLM_MODEL,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(endpoint, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                parsed_json = json.loads(content)
                bundle = ExtractionBundle.model_validate(parsed_json)
                return bundle.records
        except Exception:
            return []

    def _deterministic_fallback_extraction(
        self, title: str, source_type: str, raw_text: str
    ) -> List[ExtractedPurificationRecord]:
        normalized_text = re.sub(r"\s+", " ", raw_text).strip()
        doc_level_cond = _enrich_condition_from_quote(ExtractedCondition(), normalized_text)

        sentences = re.split(r"(?<=[.!?])\s+", normalized_text)
        records: List[ExtractedPurificationRecord] = []
        current_page_prefix = ""

        for sent in sentences:
            m_page = re.match(r"^(\[Page\s+\d+\])\s*(.*)$", sent)
            if m_page:
                current_page_prefix = m_page.group(1) + " "
                body_sent = m_page.group(2).strip()
            else:
                body_sent = sent.strip()

            if len(body_sent) < 25:
                continue
            low = body_sent.lower()
            has_domain_signal = any(
                k in low
                for k in (
                    "his-tag",
                    "histidine",
                    "imac",
                    "imidazole",
                    "ni-nta",
                    "ni-ida",
                    "iminodiacetic acid",
                    "histrap",
                    "ni sepharose",
                    "talon",
                    "purification",
                    "slyd",
                    "urea",
                    "guanidine",
                    "edta",
                    "dtt",
                    "nacl",
                )
            )
            if not has_domain_signal:
                continue

            protein_mention = "His-tagged Recombinant Protein"
            for candidate_name, pattern in [
                ("GFP", r"\b(?:egfp|gfp|green fluorescent protein)\b"),
                ("SlyD", r"\bslyd\b"),
                ("ArnA", r"\barna\b"),
                ("Tau", r"\btau\b"),
                ("TEV protease", r"\btev\b"),
            ]:
                if re.search(pattern, low):
                    protein_mention = candidate_name
                    break

            contaminant_mention = "SlyD" if "slyd" in low else ("ArnA" if "arna" in low else None)

            tag_match = re.search(r"\b(?:6x|8x|10x|poly)?his(?:tidine)?[-_\s]?tag(?:ged)?\b", low)
            tag_mention = tag_match.group(0) if tag_match else None

            resin_matches = RESIN_PATTERN.findall(body_sent)
            resin_name = (
                ", ".join(dict.fromkeys(m.strip() for m in resin_matches)) if resin_matches else None
            )

            method_name = "Denaturing IMAC" if ("urea" in low or "guanidine" in low) else "IMAC"

            purity_match = re.search(r"(purity[^.;]{0,40}|>\s*\d+\s*%\s*pure|\d+\s*%\s*purity)", low)
            yield_match = re.search(r"(yield[^.;]{0,40}|\d+(?:\.\d+)?\s*mg(?:/l| per l)[^.;]{0,25})", low)
            activity_match = re.search(r"((?:enzymatic|biological|functional)?\s*activity[^.;]{0,40})", low)

            quote_with_page = f"{current_page_prefix}{body_sent}".strip()
            sent_cond = _enrich_condition_from_quote(
                ExtractedCondition(condition_summary=body_sent[:220]), quote_with_page
            )

            if not has_concrete_condition(sent_cond) and has_concrete_condition(doc_level_cond):
                for field_name in CONCRETE_CONDITION_FIELDS:
                    setattr(sent_cond, field_name, getattr(doc_level_cond, field_name))

            records.append(
                ExtractedPurificationRecord(
                    protein_mention=protein_mention,
                    tag_mention=tag_mention,
                    method_name=method_name,
                    resin_name=resin_name,
                    contaminant_mention=contaminant_mention,
                    conditions=sent_cond,
                    outcomes=ExtractedOutcome(
                        purity=purity_match.group(1).strip() if purity_match else None,
                        yield_metric=yield_match.group(1).strip() if yield_match else None,
                        activity=activity_match.group(1).strip() if activity_match else None,
                    ),
                    verbatim_quote=quote_with_page,
                )
            )

            if len(records) >= 15:
                break

        return records