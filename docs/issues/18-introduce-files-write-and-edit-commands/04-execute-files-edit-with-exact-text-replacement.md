# feat(runtime): execute files edit with exact text replacement

**状态：** resolved

## 背景

RFC-0009 的 `files edit <path> <<'EDI' {...} EDI` 对单个文件做一次或多次精确
文本替换，是"局部修改"场景下替代 `sed -i`、整文件重写的手段。核心语义取自
pi-agent：所有 oldText 相对原始内容匹配、校验非空/唯一/互不重叠、按倒序
应用，保证偏移稳定；v1 不做 fuzzy 匹配。

## 影响

完成后，模型一次调用可修改同一文件的多个不相邻片段，匹配失败（未找到、
出现多次、重叠、空 oldText）返回带具体原因的 failed execution，不会产生
静默错误替换。BOM 与行尾（LF/CRLF）在编辑后保持原样。

## 变更

- 在 `_FileHandler` 中实现 `files edit` 分支，消费 issue 01 的 JSON payload：
  - 读取文件字节，UTF-8 解码失败即报错；
  - strip BOM 并记录，结束时重新前置；
  - 检测首个换行为 `\r\n` 或 `\n`，内容归一化为 LF；
  - 每个 edit：oldText（同样 LF 归一化）在**原始归一化内容**上精确匹配，
    要求恰好出现一次；空 oldText、未找到、多次出现分别返回独立错误
    （错误含路径与原因，对齐 pi-agent 文案）；
  - 按匹配位置升序排序检查互不重叠，重叠则报错并提示合并为一个 edit；
  - 按匹配位置倒序应用替换，保证偏移稳定；
  - 恢复行尾与 BOM，复用 issue 03 的原子写路径（保留原 mode）；
  - 成功输出 stdout 一行 `replaced <n> block(s) in <path>`。
- 写回前调用 `prepare_path`，行为与 write 一致。
- 单元测试：单处替换、多处替换、倒序应用正确性、not found / 重复 / 空
  oldText / 重叠 / 无变化五类错误、CRLF 与 BOM 保持、引号形式
  `files edit <path> '<json>'`、非法 JSON 与空 edits。

## 验收标准

- [ ] 一处调用可完成多处不相邻替换，结果与顺序无关。
- [ ] 五类匹配错误均有独立、可操作的错误信息，且文件未被部分修改。
- [ ] BOM 与行尾在编辑前后保持一致。
- [ ] in-view 路径写回前完成 copy-up，Repertoire 不受影响。
