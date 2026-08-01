"""obsidian — kg 知识图谱 → Obsidian vault 只读导出层 (计划书 P2)。

kg/graph.db (SQLite, 17 万边) 是唯一真相源, 本模块只做只读渲染:
每节点一个 md (Dataview 友好 frontmatter + [[]] 双链出边/入边),
按 node_type 分目录 (policy/ industry_tdx/ company/ product/ 等), 另生成 _index.md。
用户在 Obsidian 打开 vault 即可看 graph view + Dataview 查询。

松耦合: db 缺失 / 表为空 / jinja2 缺失或模板渲染失败 → 记 warning 优雅降级,
绝不抛异常影响主流程, 绝不写 kg/graph.db (file:...?mode=ro 只读连接)。
"""
