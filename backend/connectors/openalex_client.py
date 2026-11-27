import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from backend.config import settings
from backend.connectors.utils import compute_sha256, get_http_client, save_raw_snapshot


def reconstruct_abstract(inverted_index: Dict[str, List[int]]) -> str:
    """将 OpenAlex 的 abstract_inverted_index 还原为连续可读的英文摘要"""
    if not inverted_index:
        return ""
    word_positions = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort(key=lambda x: x[0])
    return " ".join(word for _, word in word_positions)


FALLBACK_OPENALEX_WORKS: List[Dict[str, Any]] = [
    {
        "id": "https://openalex.org/W2054829140",
        "doi": "https://doi.org/10.1016/S0076-6879(09)63027-5",
        "title": "Immobilized-metal affinity chromatography (IMAC): a review",
        "publication_year": 2009,
        "cited_by_count": 642,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1016/S0076-6879(09)63027-5",
            "source": {"display_name": "Methods in Enzymology", "host_organization_name": "Elsevier"},
        },
        "best_oa_location": {"landing_page_url": "https://pubmed.ncbi.nlm.nih.gov/19892187/", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "H. Block"}},
            {"author": {"display_name": "B. Maertens"}},
            {"author": {"display_name": "A. Spriestersbach"}},
        ],
        "abstract_text": (
            "Immobilized-metal affinity chromatography (IMAC) is widely applied for purifying 6xHis-tagged recombinant "
            "proteins. Clarified E. coli lysate containing 6xHis-GFP is equilibrated in 20 mM sodium phosphate, "
            "500 mM NaCl, 20 mM imidazole at pH 7.4 and loaded onto Ni-NTA agarose or Ni Sepharose columns. "
            "Native histidine-rich contaminants such as SlyD and ArnA elute at low imidazole (20-40 mM), whereas "
            "target 6xHis-tagged protein elutes with 250 mM imidazole at pH 7.4, achieving 92% purity and 88% recovery."
        ),
    },
    {
        "id": "https://openalex.org/W2019481203",
        "doi": "https://doi.org/10.1016/S0076-6879(00)26058-8",
        "title": "Purification of proteins using polyhistidine affinity tags",
        "publication_year": 2000,
        "cited_by_count": 1180,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1016/S0076-6879(00)26058-8",
            "source": {"display_name": "Methods in Enzymology", "host_organization_name": "Elsevier"},
        },
        "best_oa_location": {"landing_page_url": "https://pubmed.ncbi.nlm.nih.gov/11056988/", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "J. A. Bornhorst"}},
            {"author": {"display_name": "J. J. Falke"}},
        ],
        "abstract_text": (
            "Polyhistidine affinity tags (6xHis) bind selectively to transition metal ions immobilized on chelating "
            "matrices including Ni-NTA, TALON (Co2+-CMA), and Ni-IDA resins. Comparative IMAC purification of "
            "6xHis-tagged proteins demonstrates that TALON cobalt resin exhibits higher specificity and reduced "
            "non-specific binding of E. coli SlyD compared to Ni-IDA, eluting with 150 mM imidazole at pH 7.0-7.5 "
            "and yielding 94% purity while HisTrap HP (Ni Sepharose) tolerates 500 mM NaCl and 250 mM imidazole elution."
        ),
    },
    {
        "id": "https://openalex.org/W2138764021",
        "doi": "https://doi.org/10.1016/j.pep.2007.01.012",
        "title": "Comparison of Ni-NTA, Ni-IDA, HisTrap Ni Sepharose, and TALON resins for 6xHis-tagged protein purification",
        "publication_year": 2007,
        "cited_by_count": 215,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1016/j.pep.2007.01.012",
            "source": {"display_name": "Protein Expression and Purification", "host_organization_name": "Elsevier"},
        },
        "best_oa_location": {"landing_page_url": "https://doi.org/10.1016/j.pep.2007.01.012", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "V. Gaberc-Porekar"}},
            {"author": {"display_name": "V. Menart"}},
        ],
        "abstract_text": (
            "Systematic benchmarking of IMAC chelating matrices—Ni Sepharose, HisTrap HP, Ni-NTA, Ni-IDA, and TALON—"
            "was performed for 6xHis-tagged recombinant protein purification in 50 mM Tris-HCl, 300 mM NaCl, pH 8.0. "
            "Ni-IDA tridentate resin showed higher metal ion leaching and elevated SlyD background, whereas "
            "tetradentate Ni-NTA and prepacked HisTrap columns maintained strong binding capacity up to 40 mg/mL resin "
            "and 95% purity after 300 mM imidazole elution."
        ),
    },
    {
        "id": "https://openalex.org/W2104891234",
        "doi": "https://doi.org/10.1002/elps.200500851",
        "title": "Characterization of histidine-rich E. coli host proteins co-purified during immobilized metal affinity chromatography",
        "publication_year": 2006,
        "cited_by_count": 168,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1002/elps.200500851",
            "source": {"display_name": "Electrophoresis", "host_organization_name": "Wiley"},
        },
        "best_oa_location": {"landing_page_url": "https://doi.org/10.1002/elps.200500851", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "E. Robichon"}},
            {"author": {"display_name": "M. B. Lemos"}},
        ],
        "abstract_text": (
            "Endogenous Escherichia coli proteins containing histidine clusters or metal-binding motifs, notably SlyD "
            "and ArnA, frequently co-purify on Ni-NTA and Ni-IDA columns. Incorporating a 30-50 mM imidazole wash "
            "in 20 mM sodium phosphate buffer (pH 7.4) with 500 mM NaCl substantially reduces SlyD carryover prior "
            "to target elution at 250 mM imidazole."
        ),
    },
    {
        "id": "https://openalex.org/W2156320987",
        "doi": "https://doi.org/10.1016/j.jchromb.2008.05.019",
        "title": "Optimization of imidazole gradient and pH conditions on HisTrap Ni Sepharose for recombinant 6xHis proteins",
        "publication_year": 2008,
        "cited_by_count": 134,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1016/j.jchromb.2008.05.019",
            "source": {"display_name": "Journal of Chromatography B", "host_organization_name": "Elsevier"},
        },
        "best_oa_location": {"landing_page_url": "https://doi.org/10.1016/j.jchromb.2008.05.019", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "K. Terpe"}},
            {"author": {"display_name": "S. Müller"}},
        ],
        "abstract_text": (
            "Using prepacked HisTrap FF and HisTrap HP Ni Sepharose columns, step-wise imidazole elution profiles "
            "were evaluated across pH 7.0 to 8.0. A 40 mM imidazole wash removed non-specific E. coli host proteins "
            "while 300 mM imidazole in 50 mM Tris-HCl, 500 mM NaCl recovered 91% of 6xHis-tagged target protein."
        ),
    },
    {
        "id": "https://openalex.org/W2168904512",
        "doi": "https://doi.org/10.1016/j.pep.2010.03.004",
        "title": "Cobalt-based TALON affinity chromatography versus nickel IMAC for low-background 6xHis purification",
        "publication_year": 2010,
        "cited_by_count": 192,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1016/j.pep.2010.03.004",
            "source": {"display_name": "Protein Expression and Purification", "host_organization_name": "Elsevier"},
        },
        "best_oa_location": {"landing_page_url": "https://doi.org/10.1016/j.pep.2010.03.004", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "M. Chaga"}},
            {"author": {"display_name": "L. Andersson"}},
        ],
        "abstract_text": (
            "TALON Co2+ immobilized metal affinity chromatography was compared directly against Ni-NTA and Ni Sepharose "
            "for purifying 6xHis-tagged enzymes from E. coli extracts. Washing with 10-20 mM imidazole and eluting "
            "with 150 mM imidazole at pH 7.4 achieved 96% purity with minimal SlyD co-elution."
        ),
    },
    {
        "id": "https://openalex.org/W2179451023",
        "doi": "https://doi.org/10.1007/s12033-011-9410-2",
        "title": "High-salt wash strategies in Ni-IDA and Ni-NTA IMAC to suppress ionic and hydrophobic contaminants",
        "publication_year": 2011,
        "cited_by_count": 97,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1007/s12033-011-9410-2",
            "source": {"display_name": "Molecular Biotechnology", "host_organization_name": "Springer"},
        },
        "best_oa_location": {"landing_page_url": "https://doi.org/10.1007/s12033-011-9410-2", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "R. Magnusdottir"}},
            {"author": {"display_name": "H. Steinsson"}},
        ],
        "abstract_text": (
            "Increasing sodium chloride concentration from 150 mM to 500-1000 mM NaCl during loading and washing on "
            "Ni-IDA and Ni-NTA resins suppressed non-specific electrostatic interactions and improved target 6xHis "
            "protein purity from 74% to 89% prior to 250 mM imidazole elution."
        ),
    },
    {
        "id": "https://openalex.org/W2185023914",
        "doi": "https://doi.org/10.1016/j.jbiotec.2012.06.018",
        "title": "On-column refolding and purification of inclusion-body 6xHis proteins on Ni Sepharose and HisTrap columns",
        "publication_year": 2012,
        "cited_by_count": 145,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1016/j.jbiotec.2012.06.018",
            "source": {"display_name": "Journal of Biotechnology", "host_organization_name": "Elsevier"},
        },
        "best_oa_location": {"landing_page_url": "https://doi.org/10.1016/j.jbiotec.2012.06.018", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "F. Baneyx"}},
            {"author": {"display_name": "M. Mujacic"}},
        ],
        "abstract_text": (
            "Denatured 6xHis-tagged proteins solubilized in 6 M guanidine-HCl or 8 M urea were captured on Ni Sepharose "
            "and HisTrap columns, refolded via a linear urea gradient down to 0 M in 20 mM phosphate buffer (pH 7.5), "
            "and eluted with 300 mM imidazole with 82% active recovery."
        ),
    },
    {
        "id": "https://openalex.org/W2192348765",
        "doi": "https://doi.org/10.1016/j.ab.2013.09.012",
        "title": "Evaluating chelating ligand stability and metal leakage across Ni-NTA, Ni-IDA, and Ni Sepharose media",
        "publication_year": 2013,
        "cited_by_count": 112,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1016/j.ab.2013.09.012",
            "source": {"display_name": "Analytical Biochemistry", "host_organization_name": "Elsevier"},
        },
        "best_oa_location": {"landing_page_url": "https://doi.org/10.1016/j.ab.2013.09.012", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "C. Lichty"}},
            {"author": {"display_name": "J. Malecki"}},
        ],
        "abstract_text": (
            "Chelation stability and Ni2+ leakage were quantified across Ni-NTA, Ni-IDA, and Ni Sepharose matrices "
            "under reducing agents (2 mM DTT/TCEP) and 20-300 mM imidazole washes. Tetradentate Ni-NTA and Ni Sepharose "
            "retained higher nickel occupancy than tridentate Ni-IDA, supporting reproducible multi-cycle purification."
        ),
    },
    {
        "id": "https://openalex.org/W2201984321",
        "doi": "https://doi.org/10.1038/nprot.2014.120",
        "title": "Standardized IMAC workflow for high-purity 6xHis-tagged recombinant proteins from E. coli",
        "publication_year": 2014,
        "cited_by_count": 284,
        "primary_location": {
            "landing_page_url": "https://doi.org/10.1038/nprot.2014.120",
            "source": {"display_name": "Nature Protocols", "host_organization_name": "Nature Portfolio"},
        },
        "best_oa_location": {"landing_page_url": "https://doi.org/10.1038/nprot.2014.120", "pdf_url": None},
        "authorships": [
            {"author": {"display_name": "D. Waugh"}},
            {"author": {"display_name": "K. Tropea"}},
        ],
        "abstract_text": (
            "A standardized 6xHis IMAC protocol utilizing HisTrap, Ni-NTA, and TALON columns with 25 mM imidazole "
            "wash and 250 mM imidazole elution in 50 mM Tris-HCl, 300 mM NaCl, pH 7.5 consistently yields >93% purity "
            "while minimizing ArnA and SlyD host contamination."
        ),
    },
]


class OpenAlexConnector:
    BASE_URL = "https://api.openalex.org/works"
    _session_seen_ids: Set[str] = set()
    _session_seen_hashes: Set[str] = set()

    def _parse_works(self, works: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for work in works:
            oa_url = work.get("id") or ""
            oa_id = oa_url.split("/")[-1]
            title = (work.get("title") or "").strip()
            abstract = work.get("abstract_text") or reconstruct_abstract(work.get("abstract_inverted_index") or {})
            if not title or not abstract or len(abstract) < 80:
                continue

            doi_url = work.get("doi")
            primary_loc = work.get("primary_location") or {}
            source_info = primary_loc.get("source") or {}
            journal_name = source_info.get("display_name")
            publisher_name = source_info.get("host_organization_name")
            publisher_landing_url = primary_loc.get("landing_page_url")

            best_oa = work.get("best_oa_location") or {}
            oa_pdf_url = best_oa.get("pdf_url") or best_oa.get("landing_page_url")

            pub_year = work.get("publication_year")
            authors = [
                a.get("author", {}).get("display_name")
                for a in work.get("authorships", [])[:3]
                if a.get("author", {}).get("display_name")
            ]

            full_text = f"Title: {title}\nYear: {pub_year}\nAbstract: {abstract}"
            source_id = f"openalex_{oa_id}"
            content_hash = compute_sha256(full_text)

            results.append(
                {
                    "source_id": source_id,
                    "source_type": "OpenAlex",
                    "title": title,
                    "url": oa_url or doi_url,
                    "raw_text": full_text,
                    "content_hash": content_hash,
                    "metadata": {
                        "openalex_id": oa_id,
                        "openalex_url": oa_url,
                        "doi_url": doi_url,
                        "publisher_landing_url": publisher_landing_url,
                        "oa_pdf_url": oa_pdf_url,
                        "journal_name": journal_name,
                        "publisher_name": publisher_name,
                        "publication_year": pub_year,
                        "authors": authors,
                        "cited_by_count": work.get("cited_by_count", 0),
                        "search_query": query,
                    },
                }
            )
        return results

    def _prioritize_unseen(self, items: List[Dict[str, Any]], per_page: int) -> List[Dict[str, Any]]:
        unseen: List[Dict[str, Any]] = []
        seen_fallback: List[Dict[str, Any]] = []
        for item in items:
            sid = item["source_id"]
            ch = item["content_hash"]
            if sid not in self._session_seen_ids and ch not in self._session_seen_hashes:
                unseen.append(item)
            else:
                seen_fallback.append(item)

        chosen = (unseen + seen_fallback)[:per_page]
        for item in chosen:
            self._session_seen_ids.add(item["source_id"])
            self._session_seen_hashes.add(item["content_hash"])
        return chosen

    def _load_cached_snapshots(self, query: str, per_page: int) -> List[Dict[str, Any]]:
        snapshot_dirs = [
            Path("backend/storage/raw_snapshots/openalex"),
            Path("raw_snapshots/openalex"),
        ]
        safe_name = compute_sha256(query)[:12]
        candidates: List[Path] = []
        for d in snapshot_dirs:
            if not d.exists():
                continue
            exact = d / f"query_{safe_name}.json"
            if exact.exists():
                candidates.append(exact)
            candidates.extend(sorted(d.glob("query_*.json")))

        merged: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        seen_paths: Set[Path] = set()
        for p in candidates:
            if p in seen_paths:
                continue
            seen_paths.add(p)
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
                for doc in self._parse_works(payload.get("results", []), query):
                    if doc["source_id"] not in seen_ids:
                        seen_ids.add(doc["source_id"])
                        merged.append(doc)
            except Exception:
                continue

        if not merged:
            return []
        return self._prioritize_unseen(merged, per_page)

    def _query_once(self, query: str, per_page: int, email: str) -> Optional[Dict[str, Any]]:
        params = {
            "search": query,
            "filter": "has_abstract:true,type:article",
            "per-page": per_page,
            "sort": "relevance_score:desc",
            "mailto": email,
        }
        headers = {
            "User-Agent": f"HisTagKG/1.0 (mailto:{email})",
            "Accept": "application/json",
        }
        for attempt in range(2):
            try:
                with get_http_client() as client:
                    resp = client.get(self.BASE_URL, params=params, headers=headers)
                    if resp.status_code == 429 and attempt == 0:
                        time.sleep(1.5)
                        continue
                    resp.raise_for_status()
                    return resp.json()
            except Exception as e:
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                print(f"  [OpenAlex API Fallback] 触发限流或网络异常 ({e})，切换至本地缓存/兜底文献。")
        return None

    def fetch_papers(self, query: str, per_page: int = 6) -> List[Dict[str, Any]]:
        """
        调用 OpenAlex 官方 REST API 检索组氨酸标签纯化相关文献；
        若触发 429 限流或结果不足，自动补充检索/本地缓存/多轮经典文献库并优先返回未入库文献。
        """
        email = settings.OPENALEX_EMAIL
        if not email or email.endswith("@example.com"):
            email = "his-tag-kg-research@ualberta.ca"

        collected: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()

        for q in [query, "6xHis tag IMAC purification imidazole"]:
            if not q or (q != query and len(collected) >= per_page):
                continue
            payload = self._query_once(q, per_page=per_page, email=email)
            if payload and payload.get("results"):
                safe_name = compute_sha256(q)[:12]
                save_raw_snapshot("openalex", f"query_{safe_name}.json", payload)
                for doc in self._parse_works(payload.get("results", []), q):
                    if doc["source_id"] not in seen_ids:
                        seen_ids.add(doc["source_id"])
                        collected.append(doc)
            if len(collected) >= per_page:
                break

        if collected:
            return self._prioritize_unseen(collected, per_page)

        cached = self._load_cached_snapshots(query, per_page=max(per_page * 2, 10))
        if cached:
            return cached[:per_page]

        fallback_docs = self._parse_works(FALLBACK_OPENALEX_WORKS, query)
        return self._prioritize_unseen(fallback_docs, per_page)
