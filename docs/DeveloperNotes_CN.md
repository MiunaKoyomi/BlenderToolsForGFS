# BlenderToolsForGFS 开发阅读笔记（中文）

> 这份文档记录在阅读本插件源码过程中的理解与经验，方便后续维护与二次开发。所有路径均相对仓库根目录。

## 0. 名词约定

| 缩写 / 名称 | 含义 |
|---|---|
| **GMD** | Persona 5 / Dancing 系列的模型文件，本质是 GFS 容器（GMD = GFS + 头部包装）。注意：本插件代码里几乎所有"模型"概念都用 `GFS` 命名。 |
| **GFS** | 模型数据本体（骨骼、网格、材质、纹理、物理、EPL、嵌入式动画等）。`GMD = GFS header + ...`，所以导入 GMD 实际是导入 GFS。|
| **GAP** | Persona 5 的动画包文件，结构上和 GFS 几乎一致，只是其中 `animations / blend_animations / lookat_animations` 不为空。|
| **EPL** | 嵌入式/外部"附加模型"（如武器、特效母体）。|
| **NLA** | Blender 的"非线性动画"轨道。每个轨道可包含若干 strip，每个 strip 引用一个 `bpy.types.Action`。|
| **Action** | Blender 的一条动画数据块（关键帧集合）。|

## 1. 顶层目录结构

```
__init__.py                  # 插件入口：注册几乎所有 class、属性组、菜单
src/
  FileFormats/GFS/           # 纯 Python 的 GFS/GAP 二进制读写（与 Blender 解耦）
  FileFormats/TexBin/        # Metaphor：外部 TEX 贴图包解析
  BlenderIO/
    Import/                  # 把 GFS 数据"翻译"成 Blender Object
    Export/                  # 把 Blender 数据"翻译"回 GFS
    Properties/              # 自定义 PropertyGroup（动画包 / 骨骼 / 材质 / 物理 ...）
    UI/                      # 各种面板、按钮、Operator
    Utils/                   # 公共小工具（命名 / 序列化 / 动画名映射）
    Frameworks/, modelUtilsTest/  # 内部辅助框架（骨骼变换、网格管理等）
    Globals.py               # 命名空间 / 模型坐标变换常量 / 动画类型枚举
    Data.py                  # 枚举选项常量集中地
    Preferences.py           # 插件偏好设置
docs/                        # LaTeX 用户文档 + 本笔记
```

要点：
- 仓库使用 `__init__.py` 里的 `init_bpy()` 集中产出 `CLASSES`/`PROP_GROUPS`/`LIST_ITEMS`/`MODULES` 4 个元组，再在 `register()/unregister()` 里正向注册、反向注销。这种集中注册方式意味着**任何新增 Operator / 面板都要在 `__init__.py` 中显式登记**才会被 Blender 识别。
- FileFormats 层不依赖 `bpy`，BlenderIO 层才依赖 `bpy`。BlenderIO.Import 把"格式层对象"翻译成"Blender 对象 + PropertyGroup 数据"。

## 2. GMD / GFS 导入流程

入口：`src/BlenderIO/Import/Operator.py::ImportGFS.execute`
→ `import_gfs_object()`（`ImportGFS.py`）依次：

1. `import_textures`       —— 解析贴图（含外部 TEX）
2. `import_materials`      —— 构造材质
3. `ImportModel.import_model` —— 建骨架 + 网格（含 `merge_vertices`、`bone_pose`、`connect_child_bones` 等策略），并产出一张 `gfs_to_bpy_bone_map`（GFS 节点 id → Blender Bone名）
4. `create_rest_pose`      —— 单独建一条名为 `"Rest Pose"` 的 NLA 轨道（演示 T-pose，导入导出会被忽略）
5. `import_animations(..., is_external=False, gfs_to_bpy_bone_map=...)` —— 处理**内嵌**动画集
6. `import_physics / import_0x000100F8 / ImportEPLs.import_epls`
7. 给每个材质 `build_default_nodetree()`

注意：GMD 内嵌动画的 `is_external=False`，导入后会把当前动画包注册为这个模型的 "Internal GAP"（`mprops.internal_animation_pack_idx` 指向它）。

## 3. GAP 导入流程（重点关注）

入口：`src/BlenderIO/Import/Operator.py::ImportGAP`（`bl_idname = 'import_file.import_gap'`）

`import_file(context, armature, filepath)` 关键步骤：
1. 校验场景里至少有 1 个 Armature（来自下拉 `armature_name`）。
2. 通过 `GFSInterface.from_file(filepath)` 读取 GAP 字节（GAP 与 GFS 用同一种容器解析）。
3. 调用：
   ```python
   import_animations(
       gfs, armature, filename,
       is_external=True,                # GAP 视为外挂动画包，不会成为 Internal GAP
       import_policies=self.policies,
       errorlog=errorlog,
   )
   ```
4. 此时 `import_animations` 里没有传 `gfs_to_bpy_bone_map`，会通过 **骨骼 override_name** 在 `available_names` 里查表（见下文）。

### 3.1 `import_animations`（`src/BlenderIO/Import/ImportAnimations.py`）

主要做这几件事：
- `ap_props = mprops.animation_packs.add()`：**创建一个新的 GAP 包**（PropertyGroup），并把它加到 `Armature.GFSTOOLS_ModelProperties.animation_packs` 列表最后一项。
- 进入 POSE 模式后：
  - **Base 动画** 逐条调 `prop_anim_from_gfs_anim(..., BASE_ANIM_TYPE, ...)`。
  - **Blend 动画** 逐条调 `prop_anim_from_gfs_anim(..., BLEND_ANIM_TYPE, ..., is_blend=True ...)`。
  - **LookAt 动画** 在 GAP 顶层 `gfs.lookat_animations` 不为 None 时导入，会自动展开 4 个子动画（`*_right / *_left / *_up / *_down`）。
- 设置新 GAP 包的 `version`、`name`(=导入文件名)、各 `flag_*`。
- `ap_props.store_animation_pack(bpy_armature_object)`：把当前 NLA 轨道（即 "Rest Pose" 之类）快照存到 `ap_props.animations` 这个 `NLATrackWrapper` 集合里。**注意：此时还没有任何业务动画被推入 NLA。**
- 更新索引：
  ```python
  mprops.active_animation_pack_idx = len(mprops.animation_packs) - 1   # 选中刚导入的 GAP
  if not is_external:
      mprops.internal_animation_pack_idx = ...   # 只有 GMD 内嵌 GAP 才设 Internal
  ```

### 3.2 `prop_anim_from_gfs_anim`：单条动画如何变成 Action

关键逻辑：
- `prop_collection = ap_props.test_anims / test_blend_anims / test_lookat_anims`（按动画类型分桶）。
- 新建一条 `prop_anim`（含 `name`、`bounding_box`、`flag_*` 等）。
- **动作名**：`action_name = gapnames_to_nlatrack(gap_name, anim_type, prop_anim.name)`，得到形如 `{gap}|{type}|{animname}` 的字符串。这条规则在 `Utils/Animation.py` 里：
  ```
  gap_name | anim_type | anim_name
  ```
  这是后文 NLA 与 GAP 包互相识别的关键 —— 回放时通过 `gapnames_from_nlatrack` 对 NLA track 反解析即可确认它属于哪个 GAP。
- `nodes_action = bpy.data.actions.new(action_name)` —— 真正新建了一条 Blender Action。
- 遍历 `gfs_anim.node_animations`：
  - 找到这条动画对应的骨骼（用 `gfs_to_bpy_bone_map` 或 `available_names`；后者基于骨骼的 `GFSTOOLS_NodeProperties.override_name`，没有 override_name 时取 `bpy_bone.name`）。
  - 根节点（`root_node_name`）：调 `build_object_fcurves`，直接写对象级 `location / rotation_quaternion / rotation_euler / scale`。
  - 普通骨骼：调 `build_transformed_fcurves`（或 Blend 分支 `build_blend_fcurves`），通过 `parent_to_bind / parent_to_bind_blend` 把 GFS 坐标系的关键帧转换到 Blender 骨骼 bind 空间。
  - 留下来的 `material_animations / camera_animations / morph_animations / unknown_animations / extra_track_data` 会被打包成 blob 存到 `prop_anim.unimported_tracks`（保留二进制原文，将来导出再回写）。
- 调 `prop_anim.node_animation.from_action(nodes_action)`，让 PropertyGroup 记录这条 Action 的元信息（封装成 `NLAStripWrapper`）。

**核心结论：GAP 导入之后，每条动画的 Action 已经在 `bpy.data.actions` 里存在了，但都没有挂到 Armature 的 NLA 上。** 用户必须自己拖进 NLA 编辑器，或点 UI 上的 "Activate GAP" 按钮（见 §4）才能把动画推入 NLA。

## 4. GAP 与 NLA 的双向桥接

桥梁全部位于 `src/BlenderIO/Properties/AnimationPack.py::GFSToolsAnimationPackProperties`：

| 方法 | 作用 | 备注 |
|---|---|---|
| `add_to_nla bpy_object` | 把本 GAP 的 `test_anims / test_blend_anims / test_lookat_anims` 转换为 NLA 轨道并附到 `armature.animation_data.nla_tracks` 上。命名按 `gapnames_to_nlatrack` 规则。Normal 动画的 `track.mute = i != active_anim_idx`；Blend/LookAt 的 `track.mute = not prop_anim.is_active`；strip 的 `blend_type` 分别为 `REPLACE` / `ADD`。 | 同一个 GAP 多次激活会生成多份 track（命名重复）。 |
| `remove_from_nla` | 删除本 GAP 对应的所有 NLA 轨道（不含 "Rest Pose"）。 | 不动 PropertyGroup 数据。 |
| `store_animation_pack` | 把**当前 Blender 上**的 NLA 轨道快照到 `ap_props.animations`（`NLATrackWrapper` 列表）。 | 并不限制是否属于本 GAP，只是按名字过滤掉 Rest Pose。 |
| `restore_animation_pack` | 删除 NLA 上所有轨道，再把 `ap_props.animations` 重新写回 NLA。 | 用于切换 GAP 时还原快照。 |
| `update_from_nla` | 反向同步：把 NLA 上属于本 GAP 的轨道读回到 `test_anims / test_blend_anims / test_lookat_anims`（用户在 NLA 编辑器里调整后再回写）。有重名检测，重名时弹 `ShowMessageBox` 拒绝。 | 配合 `ToggleActiveAnimationPack` 的"取消激活"分支使用。 |
| `relevant_nla_to_list` | 列出属于本 GAP 的轨道，做重名校验。 | |

UI 触发入口在 `src/BlenderIO/UI/Model/AnimationsSubPanel/GAPPanel.py::ToggleActiveAnimationPack`：
- 激活：`selected_gap.add_to_nla(bpy_armature_object)` + `is_active=True`。
- 取消激活：先 `update_from_nla` 收回更改，再 `remove_from_nla`，最后 `is_active=False`。

**这就是用户当前必须手动操作的根源**：导入 GAP 只是把 N 条 `(AnimationProperties + Action)` 写入 PropertyGroup/`bpy.data.actions`，NLA 仍是空的；用户必须逐个手动 "Push Down"，或激活整个 GAP（=一次性把所有动画压入 NLA，但所有非 active 动画会被静音，需要配合 §5 的导出方式使用）。

## 5. 关于 FBX 导出的标准操作

要"一条动画一段"导出到 FBX 的常见做法：
1. 在 Armature 上为每条动画建一条 NLA strip（"Stash"），即调用 `add_to_nla`。
2. FBX 导出对话框里勾选 **Animation → All Actions**（旧称 "Include All Actions"），并把 Armature 设为导出目标。
3. Blender 会把每个 NLA strip 的 Action 当作独立的动作输出（在 Unity / UE 里会看到 N 个 Animation Clip）。

> ⚠ NLA track 的 mute 状态对 FBX 的 "All Actions" 导出**不影响**；只要 strip 存在并持有 Action 就会被导出。

## 6. 易踩坑小结

1. `Rest Pose` 是一条特殊的 NLA track，**所有 GAP 相关的方法都会跳过它**（`is_anim_restpose` 判断名字为 `"Rest Pose"`）。如果手动改名，会导致 GAP 处理逻辑误伤它。
2. NLA track 的命名严格遵循 `{gap}|{type}|{anim}`，分隔符 `|` 写死在 `Utils/Animation.py::ANIM_DELIMITER`。改分隔符会把所有旧文件搞挂。
3. 动画类型三选一：`BASE_ANIM_TYPE / BLEND_ANIM_TYPE / LOOKAT_ANIM_TYPE`（见 `Globals.py`），它们决定 `add_to_nla` 时 strip 的 blend 类型（REPLACE vs ADD），导出流程也按它分类。
4. `prop_anim.unimported_tracks` 是序列化成 base64/blob 的二进制，保存了**未被 Blender 渲染的动画数据**（材质/相机/Morph 等），导出时按对应 GAP 还原。改这里要小心，删了就回不来了。
5. `add_to_nla` 不会去重 —— 如果同一 GAP 连点两次激活按钮，会留下重复同名的 NLA track，下次 `relevant_nla_to_list` 会弹"Duplicate animation names"错误，必须手动清理。
6. GAP 导入**不会自动激活**（`is_active=False`），所以 Armature 的 NLA 列表是空的；这正是本项目要解决的痛点（见 `DeveloperNotes_CN.md` 末尾的"项目改造"小节，以及 §7）。

## 7. 改造方案：导入 GAP 时一键压入 NLA

为了支持"导入完 GAP 直接导出 FBX"的常见工作流，新增 GAP 导入环节的可选项：

- **新增** `ImportGAP.push_to_nla` 布尔属性，默认勾选。位置见 `src/BlenderIO/Import/Operator.py` 内 `class ImportGAP`。
- 在 `CUSTOM_PT_GFSAnimImportSettings` 里追加 `layout.prop(operator, 'push_to_nla')`，使该选项显示在文件浏览器右侧"Import Settings"面板。
- `import_file` 调用 `import_animations` 之后，若勾选该项：
  ```python
  mprops = armature.data.GFSTOOLS_ModelProperties
  new_gap = mprops.animation_packs[mprops.active_animation_pack_idx]
  new_gap.add_to_nla(armature)
  new_gap.is_active = True
  ```
- 该操作完成后，所有导入的动画都会自动以 NLA strip 的形式出现（Normal 全部静音以匹配 Blender "Stash" 惯例），用户在 FBX 导出对话框勾选 **All Actions** 即可在 Unity / UE 获得独立 Clip。