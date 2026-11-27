import re
from typing import Dict, Optional, Tuple


class EntityAligner:
    """
    跨源实体对齐与归一化器：
    将 OpenAlex 文献、Cytiva 手册与 UniProt 数据中的异构名称映射到统一标准节点 ID，
    使三源关于同一蛋白、纯化方法或填料柱材的知识在图谱中汇合与对比。
    """

    # 蛋白别名 -> (标准蛋白ID, 标准展示名, UniProt Accession)
    PROTEIN_ALIAS_MAP: Dict[str, Tuple[str, str, str]] = {
        "p42212": ("protein:P42212", "Green fluorescent protein (P42212)", "P42212"),
        "gfp": ("protein:P42212", "Green fluorescent protein (P42212)", "P42212"),
        "egfp": ("protein:P42212", "Green fluorescent protein (P42212)", "P42212"),
        "green fluorescent protein": ("protein:P42212", "Green fluorescent protein (P42212)", "P42212"),
        "p0a9k9": ("protein:P0A9K9", "FKBP-type peptidyl-prolyl cis-trans isomerase SlyD (P0A9K9)", "P0A9K9"),
        "slyd": ("protein:P0A9K9", "FKBP-type peptidyl-prolyl cis-trans isomerase SlyD (P0A9K9)", "P0A9K9"),
        "p77398": ("protein:P77398", "Bifunctional polymyxin resistance protein ArnA (P77398)", "P77398"),
        "arna": ("protein:P77398", "Bifunctional polymyxin resistance protein ArnA (P77398)", "P77398"),
        "p04517": ("protein:P04517", "Tobacco etch virus protease (P04517)", "P04517"),
        "tev": ("protein:P04517", "Tobacco etch virus protease (P04517)", "P04517"),
        "tau": ("protein:P10636", "Microtubule-associated protein tau (P10636)", "P10636"),
        "p10636": ("protein:P10636", "Microtubule-associated protein tau (P10636)", "P10636"),
    }

    # 纯化方法别名 -> (标准方法ID, 标准展示名)
    METHOD_NORMALIZATION: Dict[str, Tuple[str, str]] = {
        "imac": ("method:IMAC", "IMAC (Immobilized Metal Affinity Chromatography)"),
        "ni-nta": ("method:IMAC", "IMAC (Immobilized Metal Affinity Chromatography)"),
        "histrap": ("method:IMAC", "IMAC (Immobilized Metal Affinity Chromatography)"),
        "denaturing imac": ("method:Denaturing_IMAC", "Denaturing IMAC (Urea / Guanidine)"),
        "sec": ("method:SEC", "Size Exclusion Chromatography (SEC)"),
    }

    # 填料/柱材别名 -> (标准填料ID, 标准展示名)
    RESIN_NORMALIZATION: Dict[str, Tuple[str, str]] = {
        "ni sepharose": ("resin:ni_sepharose", "Ni Sepharose"),
        "histrap": ("resin:histrap", "HisTrap"),
        "ni-ida": ("resin:ni_ida", "Ni-IDA"),
        "ni2+-ida": ("resin:ni_ida", "Ni-IDA"),
        "iminodiacetic acid": ("resin:ni_ida", "Ni-IDA"),
        "ni-nta": ("resin:ni_nta", "Ni-NTA"),
        "ni2+-nta": ("resin:ni_nta", "Ni-NTA"),
        "nickel-nitrilotriacetic acid": ("resin:ni_nta", "Ni-NTA"),
        "talon": ("resin:talon", "TALON"),
        "co2+-cma": ("resin:talon", "TALON"),
    }

    def align_protein(self, mention: str, uniprot_accession: Optional[str] = None) -> Tuple[str, str, Optional[str]]:
        """
        对齐蛋白实体：
        返回 (node_id, standard_label, uniprot_accession)
        """
        if uniprot_accession:
            acc = uniprot_accession.strip().upper()
            low_acc = acc.lower()
            if low_acc in self.PROTEIN_ALIAS_MAP:
                nid, label, u_acc = self.PROTEIN_ALIAS_MAP[low_acc]
                return nid, label, u_acc
            return f"protein:{acc}", f"{mention.strip()} ({acc})", acc

        clean = re.sub(r"\b(?:6x|8x|poly)?his(?:tidine)?[-_\s]?tag(?:ged)?\b", "", mention.lower()).strip()
        clean = re.sub(r"\s+", " ", clean)

        # 精确或关键词匹配已知 UniProt 对齐表
        for alias, (nid, label, u_acc) in self.PROTEIN_ALIAS_MAP.items():
            if alias == clean or re.search(rf"\b{re.escape(alias)}\b", clean):
                return nid, label, u_acc

        slug = re.sub(r"[^a-z0-9]+", "_", clean).strip("_") or "generic_his_protein"
        return f"protein:{slug}", mention.strip() or "His-tagged Protein", None

    def normalize_method(self, method_name: str) -> Tuple[str, str]:
        """
        归一化纯化方法：
        返回 (node_id, standard_label)
        """
        low = (method_name or "IMAC").strip().lower()
        for key, val in self.METHOD_NORMALIZATION.items():
            if key in low:
                return val
        slug = re.sub(r"[^a-z0-9]+", "_", low).strip("_") or "imac"
        return f"method:{slug}", method_name.strip() or "IMAC"

    def normalize_resin(self, resin_name: Optional[str]) -> Optional[Tuple[str, str]]:
        """
        归一化填料/层析柱名称：
        将 Ni Sepharose、HisTrap、Ni-IDA、Ni-NTA、TALON 等映射到独立标准节点，
        未命中词表的填料则退化为清洗后的 slug 节点。
        返回 (node_id, standard_label) 或 None
        """
        if not resin_name or not resin_name.strip():
            return None
        clean = resin_name.strip()
        low = clean.lower()

        for key, val in self.RESIN_NORMALIZATION.items():
            if re.search(rf"\b{re.escape(key)}\b", low):
                return val

        slug = re.sub(r"[^a-z0-9]+", "_", low).strip("_")
        if not slug:
            return None
        return f"resin:{slug}", clean