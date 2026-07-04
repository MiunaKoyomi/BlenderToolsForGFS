import bpy

# Keep this in sync with src/BlenderIO/Globals.py
NAMESPACE = "gfstools"


_SCENE_PROPS = (
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

        if not (rm_empty or rm_lookat or rm_root or rm_blend):
            self.report({'WARNING'}, "No cleanup rules enabled.")
            return {'CANCELLED'}

        removed = 0
        skipped_restpose = 0
        for action in list(bpy.data.actions):
            name = action.name
            # Never delete the plugin-internal Rest Pose action
            if name == "Rest Pose":
                skipped_restpose += 1
                continue

            do_remove = False
            if rm_empty and self._action_is_empty(action):
                do_remove = True
            if not do_remove and rm_lookat and self._name_matches(name, "LOOKAT"):
                do_remove = True
            if not do_remove and rm_root and self._name_matches(name, "root"):
                do_remove = True
            if not do_remove and rm_blend and self._name_matches(name, "BLEND"):
                do_remove = True

            if do_remove:
                try:
                    bpy.data.actions.remove(action)
                    removed += 1
                except RuntimeError:
                    # Action is referenced elsewhere and cannot be removed right now
                    pass

        msg_parts = [f"Removed {removed} action(s)"]
        if skipped_restpose:
            msg_parts.append(f"({skipped_restpose} Rest Pose skipped)")
        self.report({'INFO'}, " ".join(msg_parts) + ".")
        return {'FINISHED'}


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
        col.label(text="Cleanup Rules:")
        col.prop(scene, "gfstools_clean_empty",  text="Empty (no keyframes)")
        col.prop(scene, "gfstools_clean_lookat", text="Name has LOOKAT")
        col.prop(scene, "gfstools_clean_root",   text="Name has root")
        col.prop(scene, "gfstools_clean_blend", text="Name has BLEND")

        layout.separator()
        layout.operator(GFSTOOLS_OT_clean_actions.bl_idname, icon='TRASH')


class SidePanel:
    @classmethod
    def register(cls):
        for name, prop in _SCENE_PROPS:
            setattr(bpy.types.Scene, name, prop)
        bpy.utils.register_class(GFSTOOLS_OT_clean_actions)
        bpy.utils.register_class(GFSTOOLS_PT_clean_panel)

    @classmethod
    def unregister(cls):
        bpy.utils.unregister_class(GFSTOOLS_PT_clean_panel)
        bpy.utils.unregister_class(GFSTOOLS_OT_clean_actions)
        for name, _ in _SCENE_PROPS:
            if hasattr(bpy.types.Scene, name):
                delattr(bpy.types.Scene, name)