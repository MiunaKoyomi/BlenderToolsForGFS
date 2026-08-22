import os
import re
import shutil
import tempfile
import types

import bpy
from bpy_extras.io_utils import ExportHelper

from ...FileFormats.GFS import GFSInterface, NotAGFSFileError
from ..Globals import ErrorLogger
from ..Import.ImportAnimations import import_animations
from ..Import.ScaleGFS import rescale_gfs_for_blender
from ..Preferences import get_preferences
from ..Utils.Animation import gapnames_from_nlatrack, is_anim_restpose

# Keep this in sync with src/BlenderIO/Globals.py
NAMESPACE = "gfstools"

_GAP_FILE_NAME_RE = re.compile(r"^([A-Za-z]+)(\d+)_(\d+)$")


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
    ("gfstools_char_name",
     bpy.props.StringProperty(
         name="Character Name",
         description="FBX prefix, e.g. 芳泽霞 → 芳泽霞_BF251.fbx",
         default="")),
    ("gfstools_gap_folder",
     bpy.props.StringProperty(
         name="GAP Folder",
         description="Folder of .GAP files (e.g. CHARACTER/0010/FIELD). Empty = export packs already imported on the armature",
         default="",
         subtype='DIR_PATH')),
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
        # Do not unpack. COPY/embed + unpack(REMOVE) zeros image.size and
        # later GAP FBXs in the same session would export with no maps.
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


def _gap_short_name(pack_name):
    """BF0010_251.GAP / pack BF0010_251 → BF251 (matches 芳泽霞_BF251.fbx)."""
    stem = os.path.splitext(os.path.basename(pack_name))[0]
    match = _GAP_FILE_NAME_RE.match(stem)
    if match:
        return "%s%s" % (match.group(1), match.group(3))
    return stem


def _looks_like_field_gap(pack_name):
    return _GAP_FILE_NAME_RE.match(os.path.splitext(os.path.basename(pack_name))[0]) is not None


def _find_gfs_armature(context):
    obj = context.view_layer.objects.active
    if obj is not None:
        root = obj
        while root.parent is not None:
            root = root.parent
        if root.type == 'ARMATURE' and hasattr(root.data, "GFSTOOLS_ModelProperties"):
            return root
    for candidate in bpy.data.objects:
        if candidate.type == 'ARMATURE' and hasattr(candidate.data, "GFSTOOLS_ModelProperties"):
            return candidate
    return None


def _character_export_name(context, armature):
    name = (getattr(context.scene, "gfstools_char_name", "") or "").strip()
    if name:
        return name
    if armature is not None:
        return armature.name
    return "character"


def _safe_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(".")
    return cleaned or "character"


def _import_gap_policies(context):
    prefs = get_preferences()
    return types.SimpleNamespace(
        align_quats=prefs.align_quats,
        anim_boundbox_policy=prefs.anim_boundbox_policy,
    )


def _import_gap_onto_armature(context, armature, filepath):
    warnings = []
    try:
        gfs = GFSInterface.from_file(filepath, warnings=warnings)
    except NotAGFSFileError as exc:
        return None, str(exc)
    errorlog = ErrorLogger()
    for warning_msg in warnings:
        errorlog.log_warning_message(warning_msg)
    if errorlog.has_errors():
        return None, "; ".join(err.msg for err in errorlog.errors)
    scale = getattr(context.scene, "gfstools_scale_factor", 0.01)
    rescale_gfs_for_blender(gfs, scale)
    filename = os.path.splitext(os.path.basename(filepath))[0]
    import_animations(
        gfs,
        armature,
        filename,
        is_external=True,
        import_policies=_import_gap_policies(context),
        errorlog=errorlog,
    )
    return filename, None


def _pack_by_name(armature, name):
    packs = armature.data.GFSTOOLS_ModelProperties.animation_packs
    for pack in packs:
        if pack.name == name:
            return pack
    return None


def _list_gap_files(folder):
    files = []
    if not folder or not os.path.isdir(folder):
        return files
    for name in os.listdir(folder):
        if name.startswith("."):
            continue
        if os.path.splitext(name)[1].lower() != ".gap":
            continue
        files.append(os.path.join(folder, name))
    files.sort(key=lambda path: os.path.basename(path).lower())
    return files


def _isolate_gap_nla(armature, pack, include_blend=False, include_lookat=False):
    """Put this GAP's existing Actions on NLA. Skip clips whose Action was cleaned out."""
    ad = armature.animation_data_create()
    ad.action = None
    mprops = armature.data.GFSTOOLS_ModelProperties
    for other in mprops.animation_packs:
        if other.is_active:
            other.remove_from_nla(armature)
            other.is_active = False
    for track in list(ad.nla_tracks):
        if not is_anim_restpose(track):
            ad.nla_tracks.remove(track)
        else:
            track.mute = False

    keep_types = {"BASE"}
    if include_blend:
        keep_types.update({"BLEND", "BLENDSCALE"})
    if include_lookat:
        keep_types.update({"LOOKAT", "LOOKATSCALE"})

    added = 0
    for action in bpy.data.actions:
        if action.name == "Rest Pose" or len(action.fcurves) == 0:
            continue
        gap_name, anim_type, _anim_name = gapnames_from_nlatrack(types.SimpleNamespace(name=action.name))
        if gap_name != pack.name or anim_type not in keep_types:
            continue
        track = ad.nla_tracks.new()
        track.name = action.name
        track.mute = False
        track.strips.new(action.name, 1, action)
        added += 1
    pack.is_active = True
    return added


def _copy_files(src_dir, dst_dir):
    if not src_dir or not os.path.isdir(src_dir):
        return 0
    os.makedirs(dst_dir, exist_ok=True)
    count = 0
    for name in os.listdir(src_dir):
        src = os.path.join(src_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dst_dir, name))
            count += 1
    return count


def _retarget_images_to_dir(texture_dir):
    for img in _fbx_texture_images():
        base_name = bpy.path.basename(img.name)
        stem = os.path.splitext(base_name)[0]
        if stem.lower().endswith(".dds"):
            stem = stem[:-4]
        png = os.path.abspath(os.path.join(texture_dir, stem + ".png"))
        if os.path.isfile(png):
            img.filepath = png
            img.filepath_raw = png


def _export_unity_fbx(fbx_path, use_selection=False, add_leaf_bones=False, bake_all_actions=True, tex_cache=None):
    fbx_path = os.path.abspath(fbx_path)
    if not fbx_path.lower().endswith(".fbx"):
        fbx_path = fbx_path + ".fbx"
    fbx_dir = os.path.dirname(fbx_path)
    fbx_name = os.path.splitext(os.path.basename(fbx_path))[0]
    fbm_dir = os.path.join(fbx_dir, fbx_name + ".fbm")

    for mat in bpy.data.materials:
        _connect_gfs_diffuse_to_principled(mat)

    own_cache = tex_cache is None
    cache = tex_cache or os.path.join(fbx_dir, ".gfs_tex_cache")
    written = []
    if os.path.isdir(cache) and os.listdir(cache):
        _retarget_images_to_dir(cache)
        written = [os.path.join(cache, n) for n in os.listdir(cache) if os.path.isfile(os.path.join(cache, n))]
    else:
        written = _write_fbx_png_textures(cache)

    # COPY/embed can empty the destination .fbm; keep a cache and copy after export.
    _copy_files(cache, fbm_dir)
    _patch_unity_fbx_meta(fbx_path)

    hidden, renamed, pose_saved = _prepare_unity_fbx_scene()
    try:
        bpy.ops.export_scene.fbx(
            filepath=fbx_path,
            check_existing=False,
            path_mode='COPY',
            embed_textures=False,
            add_leaf_bones=add_leaf_bones,
            use_selection=use_selection,
            use_visible=True,
            bake_anim=True,
            bake_anim_use_all_bones=True,
            bake_anim_use_nla_strips=True,
            bake_anim_use_all_actions=bake_all_actions,
            bake_anim_force_startend_keying=True,
            apply_unit_scale=True,
            apply_scale_options='FBX_SCALE_ALL',
            axis_forward='Y',
            axis_up='Z',
        )
    finally:
        _restore_unity_fbx_scene(hidden, renamed, pose_saved)

    blend_fbm = os.path.join(
        os.path.dirname(bpy.data.filepath) if bpy.data.filepath else os.getcwd(),
        fbx_name + ".fbm",
    )
    if os.path.isdir(blend_fbm) and os.path.abspath(blend_fbm) != os.path.abspath(fbm_dir):
        _copy_files(blend_fbm, fbm_dir)

    _copy_files(cache, fbm_dir)
    _patch_unity_fbx_meta(fbx_path)
    if own_cache and os.path.isdir(cache):
        try:
            shutil.rmtree(cache)
        except OSError:
            pass
    return fbx_path, written


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
        fbx_path, written = _export_unity_fbx(
            fbx_path,
            use_selection=self.use_selection,
            add_leaf_bones=self.add_leaf_bones,
            bake_all_actions=True,
        )
        fbx_name = os.path.splitext(os.path.basename(fbx_path))[0]
        self.report(
            {'INFO'},
            f"Exported {os.path.basename(fbx_path)} + {len(written)} PNG in {fbx_name}.fbm",
        )
        return {'FINISHED'}


class GFSTOOLS_OT_batch_export_gap_fbx(bpy.types.Operator):
    bl_idname = f"{NAMESPACE}.batch_export_gap_fbx"
    bl_label = "Batch Export GAP FBX"
    bl_description = (
        "One FBX per GAP (芳泽霞_BF251.fbx, 奥村春_BF253.fbx). "
        "Does not dump every imported Action into a single file"
    )
    bl_options = {'REGISTER'}

    directory: bpy.props.StringProperty(name="Output Folder", subtype='DIR_PATH')

    filter_folder: bpy.props.BoolProperty(default=True, options={'HIDDEN'})

    include_blend: bpy.props.BoolProperty(
        name="Include BLEND clips",
        default=False,
    )
    include_lookat: bpy.props.BoolProperty(
        name="Include LOOKAT clips",
        default=False,
    )
    skip_internal: bpy.props.BoolProperty(
        name="Skip GMD internal pack",
        default=True,
    )
    keep_imported: bpy.props.BoolProperty(
        name="Keep GAPs imported after export",
        default=False,
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.prop(context.scene, "gfstools_char_name")
        layout.prop(context.scene, "gfstools_gap_folder")
        layout.prop(self, "include_blend")
        layout.prop(self, "include_lookat")
        layout.prop(self, "skip_internal")
        layout.prop(self, "keep_imported")

    def execute(self, context):
        armature = _find_gfs_armature(context)
        if armature is None:
            self.report({'ERROR'}, "No GFS armature in the scene. Import a GMD first.")
            return {'CANCELLED'}

        out_dir = bpy.path.abspath(self.directory)
        if not out_dir:
            self.report({'ERROR'}, "Pick an output folder.")
            return {'CANCELLED'}
        os.makedirs(out_dir, exist_ok=True)

        char_name = _safe_filename(_character_export_name(context, armature))
        gap_folder = (getattr(context.scene, "gfstools_gap_folder", "") or "").strip()
        gap_folder = bpy.path.abspath(gap_folder) if gap_folder else ""

        mprops = armature.data.GFSTOOLS_ModelProperties
        job_names = []
        imported_names = []

        if gap_folder:
            files = _list_gap_files(gap_folder)
            if not files:
                self.report({'ERROR'}, "No .GAP files in GAP Folder: %s" % gap_folder)
                return {'CANCELLED'}
            for filepath in files:
                filename = os.path.splitext(os.path.basename(filepath))[0]
                pack = _pack_by_name(armature, filename)
                if pack is None:
                    imported, err = _import_gap_onto_armature(context, armature, filepath)
                    if err:
                        self.report({'WARNING'}, "Skip %s: %s" % (os.path.basename(filepath), err))
                        continue
                    pack = _pack_by_name(armature, imported) or mprops.animation_packs[-1]
                    imported_names.append(pack.name)
                job_names.append(pack.name)
        else:
            internal_idx = getattr(mprops, "internal_animation_pack_idx", -1)
            for i, pack in enumerate(mprops.animation_packs):
                if self.skip_internal and i == internal_idx:
                    continue
                if self.skip_internal and not _looks_like_field_gap(pack.name):
                    continue
                job_names.append(pack.name)

        if not job_names:
            self.report({'ERROR'}, "No GAP packs to export. Import GAPs or set GAP Folder.")
            return {'CANCELLED'}

        wm = context.window_manager
        wm.progress_begin(0, len(job_names))
        exported = []
        tex_cache = tempfile.mkdtemp(prefix="gfs_unity_tex_")
        try:
            for i, pack_name in enumerate(job_names):
                wm.progress_update(i)
                pack = _pack_by_name(armature, pack_name)
                if pack is None:
                    self.report({'WARNING'}, "Missing pack %s" % pack_name)
                    continue
                short = _gap_short_name(pack.name)
                fbx_path = os.path.join(out_dir, "%s_%s.fbx" % (char_name, short))
                _isolate_gap_nla(
                    armature,
                    pack,
                    include_blend=self.include_blend,
                    include_lookat=self.include_lookat,
                )
                _export_unity_fbx(fbx_path, bake_all_actions=False, tex_cache=tex_cache)
                exported.append(os.path.basename(fbx_path))
        finally:
            wm.progress_end()
            shutil.rmtree(tex_cache, ignore_errors=True)
            for item in list(mprops.animation_packs):
                if item.is_active:
                    item.remove_from_nla(armature)
                    item.is_active = False

        if gap_folder and not self.keep_imported:
            for name in imported_names:
                pack = _pack_by_name(armature, name)
                if pack is None:
                    continue
                pack.remove_from_nla(armature)
                idx = next(
                    (i for i, item in enumerate(mprops.animation_packs) if item.name == name),
                    -1,
                )
                if idx >= 0:
                    mprops.animation_packs.remove(idx)
                prefix = name + "|"
                for action in list(bpy.data.actions):
                    if action.name.startswith(prefix):
                        try:
                            bpy.data.actions.remove(action)
                        except RuntimeError:
                            pass

        self.report(
            {'INFO'},
            "Exported %d FBX (one per GAP) to %s: %s"
            % (len(exported), out_dir, ", ".join(exported[:8]) + ("..." if len(exported) > 8 else "")),
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

        box = layout.box()
        box.label(text="Batch: one FBX per GAP")
        box.prop(scene, "gfstools_char_name")
        box.prop(scene, "gfstools_gap_folder")
        box.operator(GFSTOOLS_OT_batch_export_gap_fbx.bl_idname, icon='FILE_FOLDER')

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
        _unregister_stale_class(bpy.types.Operator, GFSTOOLS_OT_batch_export_gap_fbx.bl_idname)
        for name, prop in _SCENE_PROPS:
            setattr(bpy.types.Scene, name, prop)
        bpy.utils.register_class(GFSTOOLS_OT_clean_actions)
        bpy.utils.register_class(GFSTOOLS_OT_export_fbx_with_png_textures)
        bpy.utils.register_class(GFSTOOLS_OT_batch_export_gap_fbx)
        bpy.utils.register_class(GFSTOOLS_PT_clean_panel)

    @classmethod
    def unregister(cls):
        for class_type in (
                GFSTOOLS_PT_clean_panel,
                GFSTOOLS_OT_batch_export_gap_fbx,
                GFSTOOLS_OT_export_fbx_with_png_textures,
                GFSTOOLS_OT_clean_actions):
            try:
                bpy.utils.unregister_class(class_type)
            except RuntimeError:
                pass
        for name, _ in _SCENE_PROPS:
            if hasattr(bpy.types.Scene, name):
                delattr(bpy.types.Scene, name)
