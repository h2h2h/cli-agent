# feat(capability): prepare managed view paths before file mutations

**状态：** resolved

## 背景

`_CapabilityView` 目前只在 `prepare_shell` 中对 Shell 命令做写前 copy-up 与
删除后 whiteout 调和，依赖 `_DIRECT_MUTATORS` 启发式判断命令是否变更文件。
RFC-0009 的 `files write` / `files edit` 直接由 Runtime handler 写文件，不经
Shell，因此需要一条按路径判定、与命令无关的准备通道：in-view 路径（
`.workspace/{tools,skills,library,_mcp}` 下）的 lower link 在写入前转为实体
文件，whiteout 在写入时移除。

这样 in-view 与否由目标路径决定，不再依赖命令清单，是 issue 07 删除
`_DIRECT_MUTATORS` 与 Shell 变更启发式的前提。

## 影响

完成后，`_CapabilityView` 提供 `prepare_path(path)`：普通 workspace 文件
no-op；in-view 路径自动 copy-up / 去 whiteout，Repertoire 下层文件不被穿透
修改；symlink 中间目录被拒绝。`files` handler 写回前调用它即可获得与 Shell
变更路径一致的行为，且该能力独立于命令形态、可被后续任何文件修改通道复用。

## 变更

- `_CapabilityView` 新增 `prepare_path(path: Path)`：
  - 路径不在 view 内时无操作；
  - 在 view 内时先执行 `_reject_symlink_intermediates`（复用现有实现）；
  - 目标是 lower link 时执行既有 `_copy_up`（转为 view 层实体文件）；
  - 路径不存在但存在 whiteout 时移除 whiteout（写入即取消隐藏）；
  - 不删除任何文件、不创建 whiteout（`files` 命令无删除语义）。
- 保持 `prepare_shell` 行为不变；`prepare_path` 为纯新增方法。
- 测试：
  - 普通 workspace 文件 no-op；
  - in-view lower link 写入后 view 层为实体文件且 Repertoire 内容未变；
  - whiteout 存在时写入取消隐藏；
  - symlink 中间目录路径被拒绝（`ValueError`）；
  - 新建 in-view 子目录文件（父目录为真实目录）正常。

## 验收标准

- [ ] `prepare_path` 对非 view 路径零副作用。
- [ ] in-view lower link 写入不穿透 Repertoire。
- [ ] whiteout 语义与 `prepare_shell` 一致。
- [ ] `prepare_shell` 行为无回归。
