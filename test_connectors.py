from backend.connectors.cytiva_scraper import CytivaHandbookConnector
from backend.connectors.openalex_client import OpenAlexConnector
from backend.connectors.uniprot_client import UniProtConnector
from backend.database import KGRepository


def run_connector_smoke_test():
    repo = KGRepository()
    repo.reset_database()

    print("=== 1. 测试 UniProt FASTA.gz 真实下载与解析 ===")
    uniprot = UniProtConnector()
    # 检索大肠杆菌内源高组氨酸污染蛋白 SlyD (P0A9K9)、ArnA (P77398) 以及经典模型蛋白 GFP (P42212)
    uniprot_docs = uniprot.fetch_proteome_fasta_gz(
        uniprot_query="(accession:P42212) OR (accession:P0A9K9) OR (accession:P77398)",
        dataset_label="round1_seed_proteins",
    )
    for doc in uniprot_docs:
        inserted = repo.insert_source(
            source_id=doc["source_id"],
            source_type=doc["source_type"],
            title=doc["title"],
            url=doc["url"],
            raw_text=doc["raw_text"],
            metadata=doc["metadata"],
            content_hash=doc["content_hash"],
            discovered_round=1,
        )
        print(f"  [UniProt] {doc['title']} | 入库: {inserted} | URL: {doc['url']}")

    print("\n=== 2. 测试 Cytiva 官方手册网页发现与 PDF 解析 ===")
    cytiva = CytivaHandbookConnector()
    cytiva_docs = cytiva.fetch_and_parse_handbooks(
        focus_keywords=["HisTrap", "IMAC", "imidazole"],
        max_docs=1,
    )
    for doc in cytiva_docs:
        inserted = repo.insert_source(
            source_id=doc["source_id"],
            source_type=doc["source_type"],
            title=doc["title"],
            url=doc["url"],
            raw_text=doc["raw_text"],
            metadata=doc["metadata"],
            content_hash=doc["content_hash"],
            discovered_round=1,
        )
        print(f"  [Cytiva] {doc['title']} | 提取字符数: {len(doc['raw_text'])} | 入库: {inserted}")
        print(f"           URL: {doc['url']}")

    print("\n=== 3. 测试 OpenAlex REST API 文献检索与摘要还原 ===")
    openalex = OpenAlexConnector()
    oa_docs = openalex.fetch_papers(
        query="His-tag protein purification IMAC imidazole",
        per_page=3,
    )
    for doc in oa_docs:
        inserted = repo.insert_source(
            source_id=doc["source_id"],
            source_type=doc["source_type"],
            title=doc["title"],
            url=doc["url"],
            raw_text=doc["raw_text"],
            metadata=doc["metadata"],
            content_hash=doc["content_hash"],
            discovered_round=1,
        )
        print(f"  [OpenAlex] {doc['title'][:65]}... | 入库: {inserted}")
        print(f"             URL: {doc['url']}")

    all_sources = repo.get_all_sources()
    print(f"\n✅ 联调完成！SQLite 数据库当前共收录 {len(all_sources)} 条真实去重来源资料。")


if __name__ == "__main__":
    run_connector_smoke_test()