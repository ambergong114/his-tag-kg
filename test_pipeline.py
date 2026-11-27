from backend.database import KGRepository
from backend.pipeline.orchestrator import KGIterationOrchestrator


def run_three_round_test():
    repo = KGRepository()
    repo.reset_database()
    orchestrator = KGIterationOrchestrator(repo)

    for round_num in [1, 2, 3]:
        print(f"\n==================== 执行第 {round_num} 轮迭代 ====================")
        log = orchestrator.run_iteration(round_num=round_num)
        print(f"状态: {log['status']}")
        print(f"检索理由: {log['search_rationale']}")
        print(
            f"新增资料: {log['new_sources_count']} | "
            f"新增节点: {log['new_nodes_count']} | "
            f"新增关系: {log['new_edges_count']} | "
            f"补全/融合修正: {log['refinements_count']}"
        )
        print(f"总结: {log['summary_note']}")
        if log["refinements"]:
            for r in log["refinements"][:3]:
                print(f"  [补全/对齐] {r['label']} -> {r['details']}")

    graph = repo.get_full_graph()
    print(f"\n✅ 三轮闭环结束！图谱总计 {graph['stats']['total_nodes']} 个节点，{graph['stats']['total_edges']} 条带证据关系。")


if __name__ == "__main__":
    run_three_round_test()
