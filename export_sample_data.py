import json
import shutil
from backend.config import DB_PATH, SAMPLE_DATA_DIR
from backend.database import KGRepository


def export_all():
    repo = KGRepository()
    SAMPLE_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 复制当前包含三轮完整运行结果的 SQLite 数据库
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, SAMPLE_DATA_DIR / "kg_database_sample.sqlite")

    # 2. 导出三轮运行审计记录 JSON
    logs = repo.get_iteration_logs()
    (SAMPLE_DATA_DIR / "three_round_run_logs.json").write_text(
        json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 3. 导出完整知识图谱 JSON（含节点、带原文证据的边、统计）
    graph = repo.get_full_graph()
    (SAMPLE_DATA_DIR / "knowledge_graph_export.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 4. 导出三源原始资料清单 JSON
    sources = repo.get_all_sources()
    (SAMPLE_DATA_DIR / "sources_export.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"✅ 样例数据已全部导出至: {SAMPLE_DATA_DIR}")
    print(f"   - kg_database_sample.sqlite")
    print(f"   - three_round_run_logs.json ({len(logs)} 轮记录)")
    print(f"   - knowledge_graph_export.json ({graph['stats']['total_nodes']} 节点, {graph['stats']['total_edges']} 边)")
    print(f"   - sources_export.json ({len(sources)} 条来源)")


if __name__ == "__main__":
    export_all()
