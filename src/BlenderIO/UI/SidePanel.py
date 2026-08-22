import os
import shutil

import bpy
from bpy_extras.io_utils import ExportHelper

# Keep this in sync with src/BlenderIO/Globals.py
NAMESPACE = "gfstools"


_SCENE_PROPS = (
    ("gfstools_scale_factor",
     bpy.props.FloatProperty(
         name="scaleFactor",
         description="Default scale factor for GMD and GAP imports",
         default=0.01,
         min=0.000001,
         soft_min=0.000001,
         precision=6)),
    ("gfstools_clean_empty",
     bpy.props.BoolProperty(
         name="Remove Empty Actions",
         description="Delete actions that have no fcurves / no keyframes",
         default=True)),
    ("gfstools_clean_lookat",
     bpy.props.BoolProperty(
         name="Remove 'LOOKAT' Actions",
         description="Delete actions whose name contains 'LOOKAT' (case-insensitive)",
         default=True)),
    ("gfstools_clean_root",
     bpy.props.BoolProperty(
         name="Remove 'root' Actions",
         description="Delete actions whose name contains 'root' (case-insensitive)",
         default=False)),
    ("gfstools_clean_blend",
     bpy.props.BoolProperty(
         name="Remove 'BLEND' Actions",
         description="Delete actions whose name contains 'BLEND' (case-insensitive)",
         default=False)),
)


class GFSTOOLS_OT_clean_actions(bpy.types.Operator):
    bl_idname = f"{NAMESPACE}.clean_actions"
    bl_label = "Clean Actions"
    bl_description = "Delete Blender actions matching the chosen rules (empty and/or containing 'LOOKAT' / 'root' in the name)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    @staticmethod
    def _action_is_empty(action):
        if len(action.fcurves) == 0:
            return True
        return all(len(fc.keyframe_points) == 0 for fc in action.fcurves)

    @staticmethod
    def _name_matches(name, keyword):
        return keyword.lower() in name.lower()

    def execute(self, context):
        scene = context.scene
        rm_empty  = scene.gfstools_clean_empty
        rm_lookat = scene.gfstools_clean_lookat
        rm_root   = scene.gfstools_clean_root
        rm_blend  = scene.gfstools_clean_blend

        removed, skipped = clean_actions(rm_empty, rm_lookat, rm_root, rm_blend)

        msg_parts = [f"Removed {removed} action(s)"]
        if skipped:
            msg_parts.append(f"({skipped} Rest Pose skipped)")
        self.report({'INFO'}, " ".join(msg_parts) + ".")
        return {'FINISHED'}


def _connect_gfs_diffuse_to_principled(material):
    if material is None or material.node_tree is None:
        return
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    diffuse = nodes.get("Diffuse Texture")
    if bsdf is None or diffuse is None:
        return
    base = bsdf.inputs.get("Base Color")
    if base is None:
        return
    if not base.links and "Color" in diffuse.outputs:
        links.new(diffuse.outputs["Color"], base)
    if "Metallic" in bsdf.inputs:
        bsdf.inputs["Metallic"].default_value = 0.0


def _fbx_texture_images():
    images = []
    for img in bpy.data.images:
        if img.size[0] == 0 or img.name in {"Render Result", "Viewer Node"}:
            continue
        if img.source not in {"FILE", "GENERATED"}:
            continue
        images.append(img)
    return images


def _write_fbx_png_textures(texture_dir):
    os.makedirs(texture_dir, exist_ok=True)
    written = []
    for img in _fbx_texture_images():
        base_name = bpy.path.basename(img.name)
        stem = os.path.splitext(base_name)[0]
        if stem.lower().endswith(".dds"):
            stem = stem[:-4]
        out_path = os.path.join(texture_dir, stem + ".png")

        img.save_render(out_path)
        abs_png = os.path.abspath(out_path)
        img.filepath = abs_png
        img.filepath_raw = abs_png
        if img.packed_file is not None:
            try:
                img.unpack(method='REMOVE')
            except RuntimeError:
                pass
        written.append(abs_png)
    return written


def _is_unity_assets_path(path):
    norm = os.path.normpath(path).replace("\\", "/").lower()
    return "/assets/" in (norm + "/")


def _is_gfs_outline_object(obj):
    name = obj.name.lower()
    return name.startswith("blur") or " blur" in name


def _prepare_unity_fbx_scene():
    """FBX 不能把网格和同名骨骼合成一个节点；描边 Blur 在 Unity 里会当实体网格拉丝。"""
    hidden = []
    renamed = []
    pose_saved = []
    bone_names = set()
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE':
            bone_names.update(b.name for b in obj.data.bones)

    used = {o.name for o in bpy.data.objects}
    for obj in list(bpy.data.objects):
        if obj.type == 'MESH' and _is_gfs_outline_object(obj) and not obj.hide_get():
            obj.hide_set(True)
            hidden.append(obj)
        if obj.type == 'MESH' and obj.name in bone_names:
            base = obj.name + "__mesh"
            new_name = base
            i = 1
            while new_name in used:
                new_name = "%s_%d" % (base, i)
                i += 1
            renamed.append((obj, obj.name))
            used.discard(obj.name)
            obj.name = new_name
            used.add(new_name)
    return hidden, renamed, pose_saved


def _restore_unity_fbx_scene(hidden, renamed, pose_saved):
    for obj, old_name in renamed:
        try:
            obj.name = old_name
        except Exception:
            pass
    for obj in hidden:
        try:
            obj.hide_set(False)
        except Exception:
            pass
    for obj, pose_position in pose_saved:
        try:
            obj.data.pose_position = pose_position
        except Exception:
            pass



def _patch_unity_fbx_meta(fbx_path):
    """Unity On Demand Remap 不认 PNG。导出到 Assets 时写成 Import Standard + Local。"""
    if not _is_unity_assets_path(fbx_path):
        return
    meta_path = fbx_path + ".meta"
    if os.path.isfile(meta_path):
        with open(meta_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        changed = False
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            indent = line[: len(line) - len(stripped)]
            if stripped.startswith("materialImportMode:"):
                new = indent + "materialImportMode: 1\n"
                if line != new:
                    lines[i] = new
                    changed = True
            elif stripped.startswith("materialSearch:"):
                new = indent + "materialSearch: 0\n"
                if line != new:
                    lines[i] = new
                    changed = True
            elif stripped.startswith("importAnimation:"):
                new = indent + "importAnimation: 1\n"
                if line != new:
                    lines[i] = new
                    changed = True
            elif stripped.startswith("bakeAxisConversion:"):
                new = indent + "bakeAxisConversion: 1\n"
                if line != new:
                    lines[i] = new
                    changed = True
        if changed:
            with open(meta_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.writelines(lines)
        return

    guid = os.urandom(16).hex()
    with open(meta_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "fileFormatVersion: 2\n"
            f"guid: {guid}\n"
            "ModelImporter:\n"
            "  serializedVersion: 22200\n"
            "  internalIDToNameTable: []\n"
            "  externalObjects: {}\n"
            "  materials:\n"
            "    materialImportMode: 1\n"
            "    materialName: 0\n"
            "    materialSearch: 0\n"
            "    materialLocation: 1\n"
            "  animations:\n"
            "    legacyGenerateAnimations: 4\n"
            "    bakeSimulation: 0\n"
            "    resampleCurves: 1\n"
            "    optimizeGameObjects: 0\n"
            "    removeConstantScaleCurves: 0\n"
            "    motionNodeName: \n"
            "    importAnimatedCustomProperties: 0\n"
            "    importConstraints: 0\n"
            "    animationCompression: 1\n"
            "    animationRotationError: 0.5\n"
            "    animationPositionError: 0.5\n"
            "    animationScaleError: 0.5\n"
            "    animationWrapMode: 0\n"
            "    extraExposedTransformPaths: []\n"
            "    extraUserProperties: []\n"
            "    clipAnimations: []\n"
            "    isReadable: 0\n"
            "  meshes:\n"
            "    lODScreenPercentages: []\n"
            "    globalScale: 1\n"
            "    meshCompression: 0\n"
            "    addColliders: 0\n"
            "    useSRGBMaterialColor: 1\n"
            "    sortHierarchyByName: 1\n"
            "    importVisibility: 1\n"
            "    importBlendShapes: 1\n"
            "    importCameras: 0\n"
            "    importLights: 0\n"
            "    nodeNameCollisionStrategy: 1\n"
            "    fileIdsGeneration: 2\n"
            "    swapUVChannels: 0\n"
            "    generateSecondaryUV: 0\n"
            "    useFileUnits: 1\n"
            "    keepQuads: 0\n"
            "    weldVertices: 1\n"
            "    bakeAxisConversion: 1\n"
            "    preserveHierarchy: 0\n"
            "    skinWeightsMode: 0\n"
            "    maxBonesPerVertex: 4\n"
            "    minBoneWeight: 0.001\n"
            "    optimizeBones: 1\n"
            "    meshOptimizationFlags: -1\n"
            "    indexFormat: 0\n"
            "    useFileScale: 1\n"
            "  importAnimation: 1\n"
            "  animationType: 2\n"
            "  avatarSetup: 0\n"
            "  remapMaterialsIfMaterialImportModeIsNone: 0\n"
        )


class GFSTOOLS_OT_export_fbx_with_png_textures(bpy.types.Operator, ExportHelper):
    bl_idname = f"{NAMESPACE}.export_fbx_with_png_textures"
    bl_label = "Export FBX with PNG Textures"
    bl_description = "Write PNG into {name}.fbm next to the FBX (Unity Import Standard can find them)"
    filename_ext = ".fbx"

    filter_glob: bpy.props.StringProperty(default="*.fbx", options={'HIDDEN'})

    use_selection: bpy.props.BoolProperty(
        name="Selected Objects Only",
        description="Export only selected objects",
        default=False,
    )

    add_leaf_bones: bpy.props.BoolProperty(
        name="Add Leaf Bones",
        description="Pass through Blender's FBX leaf-bone option",
        default=False,
    )

    def execute(self, context):
        fbx_path = os.path.abspath(bpy.path.abspath(self.filepath))
        if not fbx_path.lower().endswith(".fbx"):
            fbx_path = fbx_path + ".fbx"
        fbx_dir = os.path.dirname(fbx_path)
        fbx_name = os.path.splitext(os.path.basename(fbx_path))[0]
        # Blender path_mode COPY 写入的相对路径是 {fbx_name}.fbm\*.png
        # 必须用这个目录名，不能用 _textures，否则 Unity 找不到。
        fbm_dir = os.path.join(fbx_dir, fbx_name + ".fbm")

        for mat in bpy.data.materials:
            _connect_gfs_diffuse_to_principled(mat)

        written = _write_fbx_png_textures(fbm_dir)
        # 先写 meta，Unity 扫到 FBX 时就会用 Import Standard + Local。
        _patch_unity_fbx_meta(fbx_path)

        hidden, renamed, pose_saved = _prepare_unity_fbx_scene()
        try:
            bpy.ops.export_scene.fbx(
                filepath=fbx_path,
                check_existing=False,
                path_mode='COPY',
                embed_textures=True,
                add_leaf_bones=self.add_leaf_bones,
                use_selection=self.use_selection,
                use_visible=True,
                bake_anim=True,
                bake_anim_use_all_bones=True,
                bake_anim_use_nla_strips=True,
                bake_anim_use_all_actions=True,
                bake_anim_force_startend_keying=True,
                apply_unit_scale=True,
                apply_scale_options='FBX_SCALE_ALL',
                # Goo Engine 的 -Z/Y 轴转换会写坏 Bip01 Foot 的 bindpose，一只脚后跟拉丝。
                # 用 Blender 原轴导出，让 Unity Bake Axis Conversion 去转。
                axis_forward='Y',
                axis_up='Z',
            )
        finally:
            _restore_unity_fbx_scene(hidden, renamed, pose_saved)

        # COPY 有时把 .fbm 写到 .blend 旁边。再拷回 FBX 同级。
        blend_fbm = os.path.join(
            os.path.dirname(bpy.data.filepath) if bpy.data.filepath else os.getcwd(),
            fbx_name + ".fbm",
        )
        if os.path.isdir(blend_fbm) and os.path.abspath(blend_fbm) != os.path.abspath(fbm_dir):
            os.makedirs(fbm_dir, exist_ok=True)
            for name in os.listdir(blend_fbm):
                src = os.path.join(blend_fbm, name)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(fbm_dir, name))

        _patch_unity_fbx_meta(fbx_path)

        self.report(
            {'INFO'},
            f"Exported {os.path.basename(fbx_path)} + {len(written)} PNG in {fbx_name}.fbm",
        )
        return {'FINISHED'}


def _action_is_empty(action):
    if len(action.fcurves) == 0:
        return True
    return all(len(fc.keyframe_points) == 0 for fc in action.fcurves)


def _name_matches(name, keyword):
    return keyword.lower() in name.lower()


def clean_actions(rm_empty, rm_lookat, rm_root, rm_blend):
    """
    Run the action cleanup pass over bpy.data.actions.

    Returns (removed_count, skipped_restpose_count). Never raises on
    RuntimeError - actions that are referenced elsewhere are simply left
    in place.
    """
    if not (rm_empty or rm_lookat or rm_root or rm_blend):
        return 0, 0

    removed = 0
    skipped = 0
    for action in list(bpy.data.actions):
        name = action.name
        if name == "Rest Pose":
            skipped += 1
            continue

        do_remove = False
        if rm_empty and _action_is_empty(action):
            do_remove = True
        if not do_remove and rm_lookat and _name_matches(name, "LOOKAT"):
            do_remove = True
        if not do_remove and rm_root and _name_matches(name, "root"):
            do_remove = True
        if not do_remove and rm_blend and _name_matches(name, "BLEND"):
            do_remove = True

        if do_remove:
            try:
                bpy.data.actions.remove(action)
                removed += 1
            except RuntimeError:
                pass

    return removed, skipped


class GFSTOOLS_PT_clean_panel(bpy.types.Panel):
    bl_label = "GFS Action Cleaner"
    bl_idname = "GFSTOOLS_PT_clean_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GFS Tools"
    bl_options = set()

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def draw(self, context):
        scene = context.scene
        layout = self.layout

        col = layout.column(align=True)
        col.label(text="Import Settings:")
        col.prop(scene, "gfstools_scale_factor")

        layout.separator()
        layout.operator(GFSTOOLS_OT_export_fbx_with_png_textures.bl_idname, icon='EXPORT')

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Cleanup Rules:")
        col.prop(scene, "gfstools_clean_empty",  text="Empty (no keyframes)")
        col.prop(scene, "gfstools_clean_lookat", text="Name has LOOKAT")
        col.prop(scene, "gfstools_clean_root",   text="Name has root")
        col.prop(scene, "gfstools_clean_blend", text="Name has BLEND")

        layout.separator()
        layout.operator(GFSTOOLS_OT_clean_actions.bl_idname, icon='TRASH')


def _unregister_stale_class(base_type, bl_idname):
    for cls in list(base_type.__subclasses__()):
        if getattr(cls, "bl_idname", None) != bl_idname:
            continue
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


class SidePanel:
    @classmethod
    def register(cls):
        _unregister_stale_class(bpy.types.Panel, GFSTOOLS_PT_clean_panel.bl_idname)
        _unregister_stale_class(bpy.types.Operator, GFSTOOLS_OT_clean_actions.bl_idname)
        _unregister_stale_class(bpy.types.Operator, GFSTOOLS_OT_export_fbx_with_png_textures.bl_idname)
        for name, prop in _SCENE_PROPS:
            setattr(bpy.types.Scene, name, prop)
        bpy.utils.register_class(GFSTOOLS_OT_clean_actions)
        bpy.utils.register_class(GFSTOOLS_OT_export_fbx_with_png_textures)
        bpy.utils.register_class(GFSTOOLS_PT_clean_panel)

    @classmethod
    def unregister(cls):
        for class_type in (
                GFSTOOLS_PT_clean_panel,
                GFSTOOLS_OT_export_fbx_with_png_textures,
                GFSTOOLS_OT_clean_actions):
            try:
                bpy.utils.unregister_class(class_type)
            except RuntimeError:
                pass
        for name, _ in _SCENE_PROPS:
            if hasattr(bpy.types.Scene, name):
                delattr(bpy.types.Scene, name)
