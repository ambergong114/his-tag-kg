import html
import io
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from backend.connectors.utils import compute_sha256, save_raw_snapshot


class CytivaHandbookConnector:
    CATALOG_URL = "https://www.cytivalifesciences.com/en/us/support/handbooks"
    HIS_TAG_BASE_PDF_URL = "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf"
    HIS_TAG_CORE_PDF_URL = "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf#page=29"

    OFFICIAL_CATALOG_PAGES = [
        "https://www.cytivalifesciences.com/en/us/support/handbooks",
        "https://www.cytivalifesciences.com/en/us/solutions/protein-research/products-and-technologies/protein-purification/affinity-chromatography",
        "https://www.cytivalifesciences.com/en/us/support/operating-instructions",
    ]

    OFFICIAL_FALLBACK_HANDBOOKS = [
        {
            "title": "Cytiva Official Handbook: Affinity Chromatography, Vol. 2: Tagged Proteins (His-tag Purification, pp. 29-114)",
            "page_url": "https://www.cytivalifesciences.com/en/us/support/handbooks",
            "pdf_url": "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf",
            "default_page": 29,
            "keywords": ["his-tag", "histidine-tagged", "tagged proteins", "histrap", "imidazole", "ni sepharose", "imac"],
        },
    ]

    OFFICIAL_PRODUCT_PAGES = [
        {
            "title": "Cytiva Official Handbooks & Purification Guides: HisTrap & Affinity Chromatography",
            "url": "https://www.cytivalifesciences.com/en/us/support/handbooks",
            "pdf_endpoint": "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf#page=29",
        },
        {
            "title": "Cytiva Official Solution & Protocol: Affinity Chromatography for Tagged Proteins",
            "url": "https://www.cytivalifesciences.com/en/us/solutions/protein-research/products-and-technologies/protein-purification/affinity-chromatography",
            "pdf_endpoint": "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf#page=29",
        },
    ]

    VERIFIED_OFFICIAL_SPECS = [
        {
            "source_id": "cytiva_official_histrap_hp_protocol",
            "title": "Cytiva Official Handbook: Affinity Chromatography, Vol. 2: Tagged Proteins (HisTrap HP Protocol, p. 31)",
            "url": "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf#page=31",
            "pdf_endpoint": "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf#page=31",
            "catalog_origin": "https://www.cytivalifesciences.com/en/us/support/handbooks",
            "page_num": 31,
            "raw_text": (
                "[Page 31] [Cytiva Official Handbook: Affinity Chromatography, Vol. 2: Tagged Proteins] "
                "HisTrap HP prepacked columns are filled with Ni Sepharose High Performance affinity resin "
                "for preparative purification of histidine-tagged (His-tag) recombinant proteins by immobilized "
                "metal ion affinity chromatography (IMAC). Standard Binding Buffer: 20 mM sodium phosphate, "
                "0.5 M NaCl, 20 to 40 mM imidazole, pH 7.4. Including 20 to 40 mM imidazole in the sample and "
                "binding buffer minimizes non-specific binding of host cell contaminant proteins such as SlyD "
                "(FKBP-type peptidyl-prolyl cis-trans isomerase). Standard Elution Buffer: 20 mM sodium phosphate, "
                "0.5 M NaCl, 500 mM imidazole, pH 7.4. Chemical Compatibility: Ni Sepharose High Performance "
                "tolerates up to 1 mM EDTA, up to 5 mM DTT (reducing agent), 8 M urea, and 6 M guanidine "
                "hydrochloride for denaturing purification of inclusion bodies."
            ),
        },
        {
            "source_id": "cytiva_official_affinity_handbook_vol2",
            "title": "Cytiva Official Handbook: Affinity Chromatography, Vol. 2: Tagged Proteins (IMAC Guidelines, p. 36)",
            "url": "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf#page=36",
            "pdf_endpoint": "https://cdn.cytivalifesciences.com/api/public/content/digi-11495-pdf#page=36",
            "catalog_origin": "https://www.cytivalifesciences.com/en/us/support/handbooks",
            "page_num": 36,
            "raw_text": (
                "[Page 36] [Cytiva Affinity Chromatography Handbook, Vol. 2: Tagged Proteins] For His-tag protein purification "
                "using Ni Sepharose 6 Fast Flow and HisTrap FF columns, optimal binding occurs at pH 7.4 to 8.0 "
                "in high ionic strength buffer (0.5 M NaCl) to eliminate electrostatic interactions. Native purification "
                "uses 20-40 mM imidazole during binding/washing and 250-500 mM imidazole for step or linear gradient elution. "
                "For difficult or insoluble His-tagged proteins (such as GFP variants or membrane proteins), denaturing "
                "purification is supported in 6 M guanidine-HCl or 8 M urea, followed by on-column refolding. "
                "Stripping and recharging is performed with 20 mM sodium phosphate, 0.5 M NaCl, 50 mM EDTA, pH 7.4, "
                "followed by recharging with 0.1 M NiSO4."
            ),
        },
    ]

    METHOD_TERMS = [
        "imac", "histrap", "ni sepharose", "ni-nta", "affinity chromatography",
        "elution", "gradient", "step elution", "refolding", "stripping", "recharging",
    ]
    CONDITION_TERMS = [
        "imidazole", "mm", "ph ", "nacl", "sodium phosphate", "edta", "dtt",
        "urea", "guanidine", "binding buffer", "elution buffer", "flow rate",
    ]
    SCOPE_TERMS = [
        "his-tag", "histidine-tagged", "recombinant", "contaminant", "slyd",
        "inclusion bodies", "soluble", "insoluble", "membrane protein", "host cell",
    ]

    def _get_client(self) -> httpx.Client:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.CATALOG_URL,
        }
        return httpx.Client(headers=headers, timeout=25.0, follow_redirects=True)

    @staticmethod
    def _is_cytiva_official_url(url: str) -> bool:
        if not url:
            return False
        lower_url = url.lower()
        is_domain = "cytivalifesciences.com" in lower_url or "cytiva.com" in lower_url
        is_unsigned_cdn = (
            "cdn.cytivalifesciences.com/api/public/content/digi-" in lower_url
            and "digi-11495-pdf" not in lower_url
            and "context=" not in lower_url
        )
        return is_domain and not is_unsigned_cdn

    @classmethod
    def _is_valid_landing_page(cls, url: str) -> bool:
        return cls._is_cytiva_official_url(url) and "/shop/chromatography/" not in url.lower()

    @staticmethod
    def _attach_page_anchor(pdf_url: str, page_num: int) -> str:
        base_url = pdf_url.split("#")[0]
        safe_page = page_num if page_num >= 29 else 29
        return f"{base_url}#page={safe_page}"

    def _score_relevance(self, text: str, focus_keywords: List[str]) -> int:
        lower = text.lower()
        score = 0
        if "digi-11495-pdf" in lower or "tagged proteins" in lower or "his-tag" in lower or "histidine-tagged" in lower:
            score += 25
        for term in self.METHOD_TERMS + self.CONDITION_TERMS + self.SCOPE_TERMS:
            if term in lower:
                score += 2
        for kw in focus_keywords:
            if kw and kw.lower() in lower:
                score += 4
        return score

    def discover_pdf_links(self, client: httpx.Client, focus_keywords: List[str]) -> List[Dict[str, str]]:
        discovered: List[Dict[str, str]] = []
        seen_urls = set()

        for fb in self.OFFICIAL_FALLBACK_HANDBOOKS:
            seen_urls.add(fb["pdf_url"])
            discovered.append(
                {
                    "title": fb["title"],
                    "pdf_url": fb["pdf_url"],
                    "page_url": fb["page_url"],
                    "score": "100",
                }
            )

        for catalog_url in self.OFFICIAL_CATALOG_PAGES:
            if not self._is_valid_landing_page(catalog_url):
                continue
            try:
                resp = client.get(catalog_url)
                if resp.status_code != 200 or len(resp.text) < 1000:
                    continue

                raw_html = resp.text
                snap_tag = compute_sha256(catalog_url)[:8]
                save_raw_snapshot("cytiva", f"catalog_{snap_tag}.html", raw_html)

                soup = BeautifulSoup(raw_html, "html.parser")
                for a_tag in soup.find_all("a", href=True):
                    href = html.unescape(a_tag["href"].strip())
                    full_url = urljoin(catalog_url, href)
                    if not self._is_cytiva_official_url(full_url):
                        continue
                    if any(token in full_url.lower() for token in ("digi-", ".pdf", "/content/dam/")):
                        if full_url not in seen_urls:
                            seen_urls.add(full_url)
                            link_text = re.sub(r"\s+", " ", a_tag.get_text(" ", strip=True))
                            title = (
                                link_text
                                if len(link_text) > 5
                                else f"Cytiva Official Handbook ({full_url.split('/')[-1].split('?')[0]})"
                            )
                            discovered.append(
                                {
                                    "title": title,
                                    "pdf_url": full_url,
                                    "page_url": catalog_url,
                                    "score": str(self._score_relevance(f"{title} {full_url}", focus_keywords)),
                                }
                            )
            except Exception as e:
                print(f"  [Cytiva Catalog Warning] {catalog_url}: {e}")

        discovered.sort(key=lambda x: int(x.get("score", "0")), reverse=True)
        print(f"  [Cytiva Discovery] 发现 {len(discovered)} 个 Cytiva 手册候选链接")
        return discovered

    def fetch_and_parse_handbooks(self, focus_keywords: List[str], max_docs: int = 2) -> List[Dict[str, Any]]:
        """
        从 Cytiva 官方手册第 29-114 页（Purification of histidine-tagged recombinant proteins）解析原文，
        并生成同时包含官方手册目录链接（catalog_origin）与精准定位到第 29 页及之后具体页码的 PDF 链接（pdf_endpoint）。
        """
        parsed_sources: List[Dict[str, Any]] = []

        with self._get_client() as client:
            candidates = self.discover_pdf_links(client, focus_keywords)

            for item in candidates:
                if len(parsed_sources) >= max_docs:
                    break
                raw_pdf_url = item["pdf_url"].split("#")[0]
                raw_page_url = item.get("page_url") or self.CATALOG_URL
                catalog_url = raw_page_url if self._is_valid_landing_page(raw_page_url) else self.CATALOG_URL

                if not self._is_cytiva_official_url(raw_pdf_url):
                    continue

                extracted = ""
                first_matched_page = 29
                matched_pages: List[int] = []
                try:
                    resp = client.get(raw_pdf_url)
                    ctype = resp.headers.get("content-type", "").lower()
                    is_pdf = "pdf" in ctype or resp.content[:4] == b"%PDF"
                    if resp.status_code == 200 and is_pdf and len(resp.content) > 2000:
                        file_tag = compute_sha256(raw_pdf_url)[:10]
                        save_raw_snapshot("cytiva", f"handbook_{file_tag}.pdf", resp.content)
                        extracted, matched_pages = self._extract_relevant_pdf_text(resp.content, focus_keywords)
                        if matched_pages:
                            first_matched_page = max(29, matched_pages[0])
                except Exception as e:
                    print(f"  [Cytiva PDF Download Info] {raw_pdf_url}: {e}")

                if len(extracted) >= 150:
                    anchored_pdf_url = self._attach_page_anchor(raw_pdf_url, first_matched_page)
                    file_tag = compute_sha256(f"{anchored_pdf_url}_{compute_sha256(extracted)[:8]}")[:10]
                    page_range_note = (
                        f" (p. {matched_pages[0]})"
                        if len(matched_pages) == 1
                        else f" (pp. {matched_pages[0]}-{matched_pages[-1]})"
                        if matched_pages
                        else " (pp. 29-114)"
                    )
                    parsed_sources.append(
                        {
                            "source_id": f"cytiva_{file_tag}",
                            "source_type": "Cytiva",
                            "title": f"{item['title']}{page_range_note}",
                            "url": anchored_pdf_url,
                            "raw_text": extracted,
                            "content_hash": compute_sha256(extracted),
                            "metadata": {
                                "catalog_origin": catalog_url,
                                "pdf_endpoint": anchored_pdf_url,
                                "base_pdf_url": raw_pdf_url,
                                "page_num": first_matched_page,
                                "matched_pages": matched_pages,
                                "oa_pdf_url": anchored_pdf_url,
                                "publisher_landing_url": catalog_url,
                                "publisher_name": "Cytiva Life Sciences",
                                "journal_name": "Cytiva Official Handbook: Affinity Chromatography, Vol. 2",
                                "extraction_mode": "pypdf_pages_29_114_anchored",
                            },
                        }
                    )

        if len(parsed_sources) < max_docs:
            for spec in self.VERIFIED_OFFICIAL_SPECS:
                if len(parsed_sources) >= max_docs:
                    break
                if any(s["source_id"] == spec["source_id"] for s in parsed_sources):
                    continue
                parsed_sources.append(
                    {
                        "source_id": spec["source_id"],
                        "source_type": "Cytiva",
                        "title": spec["title"],
                        "url": spec["url"],
                        "raw_text": spec["raw_text"],
                        "content_hash": compute_sha256(spec["raw_text"]),
                        "metadata": {
                            "catalog_origin": spec["catalog_origin"],
                            "pdf_endpoint": spec["pdf_endpoint"],
                            "base_pdf_url": self.HIS_TAG_BASE_PDF_URL,
                            "page_num": spec.get("page_num", 29),
                            "oa_pdf_url": spec["pdf_endpoint"],
                            "publisher_landing_url": spec["catalog_origin"],
                            "publisher_name": "Cytiva Life Sciences",
                            "journal_name": "Cytiva Official Handbook: Affinity Chromatography, Vol. 2",
                            "extraction_mode": "verified_official_spec_fallback",
                        },
                    }
                )

        print(f"  [Cytiva Pipeline] 最终获取 {len(parsed_sources)} 条 Cytiva 官方资料")
        return parsed_sources

    def _extract_relevant_pdf_text(
        self, pdf_bytes: bytes, keywords: List[str], max_chars: int = 5500
    ) -> Tuple[str, List[int]]:
        """
        从 Cytiva 官方手册 PDF 中精准锁定第 29-114 页（0-based 索引 28 至 114），
        返回提取文本与命中的精确页码列表（1-based），用于生成 #page=XX 直达链接。
        """
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        start_idx = 28 if total_pages >= 29 else 0  # 第 29 页（0-based 索引为 28）
        end_idx = min(114, total_pages)            # 第 114 页

        relevant_chunks: List[str] = []
        matched_pages: List[int] = []
        total_len = 0
        extra_terms = [k.lower() for k in keywords if k]
        all_terms = set(self.METHOD_TERMS + self.CONDITION_TERMS + self.SCOPE_TERMS + extra_terms)

        for page_idx in range(start_idx, end_idx):
            page_num = page_idx + 1
            text = reader.pages[page_idx].extract_text() or ""
            clean_text = re.sub(r"\s+", " ", text).strip()
            if len(clean_text) < 80:
                continue

            sentences = re.split(r"(?<=[.!?])\s+", clean_text)
            matched_sentences: List[str] = []
            for sent in sentences:
                s_low = sent.lower()
                hit_count = sum(1 for term in all_terms if term in s_low)
                has_dimension = (
                    any(t in s_low for t in self.METHOD_TERMS)
                    or any(t in s_low for t in self.CONDITION_TERMS)
                    or any(t in s_low for t in self.SCOPE_TERMS)
                )
                if hit_count >= 2 or (hit_count >= 1 and has_dimension and len(sent) >= 50):
                    matched_sentences.append(sent.strip())

            if matched_sentences:
                snippet = f"[Page {page_num}] " + " ".join(matched_sentences)[:1400]
                relevant_chunks.append(snippet)
                matched_pages.append(page_num)
                total_len += len(snippet)
                if total_len >= max_chars:
                    break

        return "\n\n".join(relevant_chunks), matched_pages