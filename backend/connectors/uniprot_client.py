import gzip
import io
import re
from typing import Any, Dict, List

from backend.connectors.utils import compute_sha256, get_http_client, save_raw_snapshot


class UniProtConnector:
    STREAM_URL = "https://rest.uniprot.org/uniprotkb/stream"

    def fetch_proteome_fasta_gz(
        self,
        uniprot_query: str,
        dataset_label: str = "ecoli_histag_targets",
    ) -> List[Dict[str, Any]]:
        """
        下载并解析 UniProt 官方 FASTA.gz 文件：
        1. 调用 UniProt REST stream API 获取 gzip 压缩的 FASTA 文件
        2. 保存原始 .fasta.gz 到本地缓存
        3. 流式解压并解析 Accession ID、蛋白名、基因名、物种及天然连续组氨酸(His-cluster)特征
        """
        params = {
            "query": uniprot_query,
            "format": "fasta",
            "compressed": "true",
        }

        with get_http_client() as client:
            resp = client.get(self.STREAM_URL, params=params)
            resp.raise_for_status()
            gz_bytes = resp.content

        # 保存真实的 .fasta.gz 文件到本地
        save_raw_snapshot("uniprot", f"{dataset_label}.fasta.gz", gz_bytes)

        # 解压 FASTA.gz 内容
        with gzip.GzipFile(fileobj=io.BytesIO(gz_bytes)) as gz_file:
            fasta_text = gz_file.read().decode("utf-8", errors="replace")

        return self._parse_fasta_records(fasta_text, uniprot_query)

    def _parse_fasta_records(self, fasta_text: str, query: str) -> List[Dict[str, Any]]:
        """解析标准 UniProt FASTA Header 与氨基酸序列"""
        entries: List[Dict[str, Any]] = []
        blocks = fasta_text.strip().split("\n>")

        for idx, block in enumerate(blocks):
            if not block.strip():
                continue
            if idx == 0 and block.startswith(">"):
                block = block[1:]

            lines = block.splitlines()
            header = lines[0].strip()
            sequence = "".join(line.strip() for line in lines[1:])

            # 解析 >sp|P0A9K9|SLYD_ECOLI FKBP-type peptidyl-prolyl cis-trans isomerase SlyD OS=Escherichia coli (strain K12) OX=83333 GN=slyD PE=1 SV=1
            parts = header.split("|")
            if len(parts) < 3:
                continue

            db_type = parts[0]  # sp 或 tr
            accession = parts[1].strip()
            rest = parts[2].strip()

            entry_name = rest.split(" ")[0]
            rest_after_entry = rest[len(entry_name) :].strip()

            protein_name_match = re.match(r"^(.*?)\s+OS=", rest_after_entry)
            protein_name = protein_name_match.group(1).strip() if protein_name_match else rest_after_entry

            os_match = re.search(r"OS=(.*?)(?:\s+OX=|\s+GN=|\s+PE=|$)", rest_after_entry)
            organism = os_match.group(1).strip() if os_match else "Unknown"

            gn_match = re.search(r"GN=([^\s]+)", rest_after_entry)
            gene_name = gn_match.group(1).strip() if gn_match else None

            # 分析序列中的天然连续组氨酸或富含组氨酸区段（解释 IMAC 非特异性共洗脱污染的关键特征）
            his_count = sequence.count("H")
            max_consecutive_his = max((len(m) for m in re.findall(r"H+", sequence)), default=0)
            c_term_tail = sequence[-25:] if len(sequence) >= 25 else sequence
            is_native_imac_contaminant = (
                max_consecutive_his >= 4 or c_term_tail.count("H") >= 6
            )

            uniprot_url = f"https://www.uniprot.org/uniprotkb/{accession}/entry"
            summary_text = (
                f"UniProt Entry: {accession} ({entry_name})\n"
                f"Protein Name: {protein_name}\n"
                f"Gene: {gene_name or 'N/A'}\n"
                f"Organism: {organism}\n"
                f"Sequence Length: {len(sequence)} aa | Total Histidines: {his_count} | "
                f"Max Consecutive His: {max_consecutive_his}\n"
                f"C-terminal 25aa: {c_term_tail}\n"
                f"Native Metal-Binding Contaminant Risk: {'High (His-rich motif)' if is_native_imac_contaminant else 'Low'}"
            )

            entries.append(
                {
                    "source_id": f"uniprot_{accession}",
                    "source_type": "UniProt",
                    "title": f"{protein_name} ({accession})",
                    "url": uniprot_url,
                    "raw_text": summary_text,
                    "content_hash": compute_sha256(f"{accession}_{sequence}"),
                    "metadata": {
                        "accession": accession,
                        "entry_name": entry_name,
                        "db_type": db_type,
                        "protein_name": protein_name,
                        "gene_name": gene_name,
                        "organism": organism,
                        "sequence_length": len(sequence),
                        "his_count": his_count,
                        "max_consecutive_his": max_consecutive_his,
                        "is_native_imac_contaminant": is_native_imac_contaminant,
                        "search_query": query,
                    },
                }
            )

        return entries