from __future__ import annotations

"""Havok importer for HKX/HKT/HKA/IGZ/PAK packfiles."""

from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

import bpy
import mathutils
from bpy_extras.io_utils import ImportHelper, axis_conversion
from mathutils import Matrix, Vector

from ..io import parsers
from ..io.igz_port import constants as igz_constants
from ..io.parsers import (
    HavokPack,
    load_from_path,
    load_igz_bytes,
    SUPPORTED_EXTENSIONS,
    PAK_PROFILE_NAMES,
    PAK_PLATFORM_ENDIANNESS,
)

_PAK_PROFILE_ITEMS = (
    [
        (name, name.replace("_", " "), f"Use the {name} PAK layout")
        for name in PAK_PROFILE_NAMES
    ]
    if PAK_PROFILE_NAMES
    else [("AUTO", "Auto", "PAK import profile list is unavailable in this build")]
)


class HavokPakEntry(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    path: bpy.props.StringProperty()
    size: bpy.props.IntProperty()
    mode: bpy.props.StringProperty()
    is_dir: bpy.props.BoolProperty()
    depth: bpy.props.IntProperty()


class HavokAnimationEntry(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    index: bpy.props.IntProperty()
    duration: bpy.props.FloatProperty()
    num_tracks: bpy.props.IntProperty()


def _refresh_pak_entries(self, _context):  # pragma: no cover - UI callback
    if not self.filepath.lower().endswith(".pak"):
        return

    # Some Blender versions construct RNA proxy objects that may not expose
    # Python-only helpers until the operator is fully instantiated. Be
    # defensive so the UI callback never raises while the operator is being
    # created or refreshed.
    loader = getattr(self, "_load_pak_entries", None)
    if callable(loader):
        loader()


def _on_active_pak_changed(self, _context):  # pragma: no cover - UI callback
    if not getattr(self, "pak_entries", None):
        return
    if self.pak_active_index < 0 or self.pak_active_index >= len(self.pak_entries):
        return
    item = self.pak_entries[self.pak_active_index]
    if not item.is_dir:
        self.archive_entry = item.path or item.name
        refresher = getattr(self, "_refresh_animation_metadata", None)
        if callable(refresher):
            refresher()


def _build_pak_tree(entries: List[parsers.PakEntry]) -> List[Dict[str, object]]:
    """Flattened directory tree for UI presentation."""

    root: Dict[str, object] = {"children": {}, "depth": -1}

    for entry in entries:
        parts = [p for p in entry.name.replace("\\", "/").split("/") if p]
        if not parts:
            parts = [entry.name]

        node = root
        for depth, part in enumerate(parts):
            children: Dict[str, Dict[str, object]] = node.setdefault("children", {})  # type: ignore[assignment]
            if part not in children:
                children[part] = {
                    "name": part,
                    "path": "/".join(parts[: depth + 1]),
                    "children": {},
                    "depth": depth,
                    "is_dir": True,
                }
            node = children[part]

        node.update(
            {
                "is_dir": False,
                "size": entry.size,
                "mode": hex(entry.mode),
            }
        )

    ordered: List[Dict[str, object]] = []

    def walk(current: Dict[str, object]) -> None:
        for key in sorted(current.get("children", {}).keys()):
            child: Dict[str, object] = current["children"][key]  # type: ignore[index]
            ordered.append(child)
            walk(child)

    walk(root)
    return ordered


class HAVOK_UL_pak_entries(bpy.types.UIList):
    bl_idname = "HAVOK_UL_pak_entries"

    def draw_item(
        self, _context, layout, _data, item, _icon, _active_data, _active_propname
    ):  # pragma: no cover - UI
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row()
            indent_row = row.row()
            for _ in range(max(item.depth, 0)):
                indent_row.separator_spacer()
            indent_row.label(
                text=item.name, icon="FILE_FOLDER" if item.is_dir else "FILE_ARCHIVE"
            )
            if not item.is_dir:
                row.label(text=f"{item.size} bytes")
                row.label(text=item.mode)
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text=item.name)


class HAVOK_UL_animation_entries(bpy.types.UIList):
    bl_idname = "HAVOK_UL_animation_entries"

    def draw_item(
        self, _context, layout, _data, item, _icon, _active_data, _active_propname
    ):  # pragma: no cover - UI
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row()
            row.label(text=item.name, icon="ACTION")
            row.label(text=f"{item.duration:.2f}s")
            row.label(text=f"{item.num_tracks} tracks")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text=item.name)


class HAVOK_OT_import(bpy.types.Operator, ImportHelper):
    """Import Havok HKX/HKT/HKA/IGZ/PAK data into the current scene."""

    bl_idname = "havok.import_hkx"
    bl_label = "Import Havok (.hkx/.hkt/.hka/.igz/.pak)"
    bl_options = {"UNDO"}

    filename_ext = ".hkx"
    filter_glob: bpy.props.StringProperty(
        default="*.hkx;*.hkt;*.hka;*.igz;*.pak",
        options={"HIDDEN"},
    )

    pak_entries: bpy.props.CollectionProperty(type=HavokPakEntry)
    pak_active_index: bpy.props.IntProperty(update=_on_active_pak_changed)
    last_pak_path: bpy.props.StringProperty(options={"HIDDEN"})
    last_pak_profile: bpy.props.StringProperty(options={"HIDDEN"})
    last_pak_platform: bpy.props.StringProperty(options={"HIDDEN"})
    animation_count: bpy.props.IntProperty(options={"HIDDEN"}, default=0)
    animation_names: bpy.props.StringProperty(options={"HIDDEN"}, default="")
    animation_metadata_key: bpy.props.StringProperty(options={"HIDDEN"}, default="")

    animation_entries: bpy.props.CollectionProperty(type=HavokAnimationEntry)
    animation_active_index: bpy.props.IntProperty()

    pak_profile: bpy.props.EnumProperty(
        name="Game version",
        description="Choose the exact PAK layout for the game you are importing",
        items=_PAK_PROFILE_ITEMS,
        default=_PAK_PROFILE_ITEMS[0][0],
        update=_refresh_pak_entries,
    )
    pak_platform: bpy.props.EnumProperty(
        name="Platform",
        description="Pick the platform endianness that matches the dump you are importing",
        items=[(key, label, label) for key, label in PAK_PLATFORM_ENDIANNESS.items()],
        default="little",
        update=_refresh_pak_entries,
    )

    archive_entry: bpy.props.StringProperty(
        name="Archive entry",
        description=(
            "Optional entry name inside PAK/ZIP archives. Leave empty to auto-select the "
            "first Havok payload."
        ),
        default="",
    )

    animation_index: bpy.props.IntProperty(
        name="Animation index",
        description=(
            "Choose which animation to import when the source contains multiple motions. "
            "Use -1 to import every animation."
        ),
        default=-1,
        min=-1,
    )

    igz_build_meshes: bpy.props.BoolProperty(
        name="Build meshes",
        description="Generate meshes when importing IGZ payloads",
        default=igz_constants.dBuildMeshes,
    )
    igz_build_bones: bpy.props.BoolProperty(
        name="Build bones",
        description="Create an armature from IGZ skeleton data",
        default=igz_constants.dBuildBones,
    )
    igz_build_faces: bpy.props.BoolProperty(
        name="Build faces",
        description="Include index buffers when creating IGZ meshes",
        default=igz_constants.dBuildFaces,
    )
    igz_allow_wii: bpy.props.BoolProperty(
        name="Allow Wii models",
        description="Permit Wii IGZ assets that may be unstable",
        default=igz_constants.dAllowWii,
    )
    igz_model_threshold: bpy.props.IntProperty(
        name="Model threshold",
        description="Limit the number of IGZ models imported before prompting",
        default=igz_constants.dModelThreshold,
        min=1,
    )
    igz_first_object_offset: bpy.props.IntProperty(
        name="First object offset",
        description=(
            "Override the offset of the first IGObject. Use -1 to import all "
            "objects without skipping."
        ),
        default=igz_constants.dFirstObjectOffset,
        min=-1,
    )

    import_meshes: bpy.props.BoolProperty(
        name="Import static meshes",
        default=True,
        description="Build Blender meshes from Havok geometry when available",
    )

    import_skeleton: bpy.props.BoolProperty(
        name="Import skeleton",
        default=True,
        description="Create an armature from the Havok skeleton definition",
    )

    animation_scale: bpy.props.FloatProperty(
        name="Animation Scale",
        description="Additional scaling factor for animation translation tracks",
        default=1.0,
        min=0.0001,
    )

    animation_import_mode: bpy.props.EnumProperty(
        name="Animation Import Mode",
        description="Choose how imported animation data is stored in Blender",
        items=[
            (
                "ACTION_PUSH_NLA",
                "Action + Push to NLA",
                "Create a sparse editable Action, push it to NLA, and clear the active Action",
            ),
            (
                "ACTION_ONLY",
                "Action Only",
                "Create a sparse editable Action and keep it as the active Action",
            ),
            (
                "BAKED_DENSE_ACTION",
                "Baked Dense Action",
                "Bake location, rotation, and scale for every animated bone on every decoded sample",
            ),
        ],
        default="ACTION_PUSH_NLA",
    )

    animation_scale_mode: bpy.props.EnumProperty(
        name="Scale Tracks",
        description="Choose when scale curves should be written in sparse animation modes",
        items=[
            (
                "AUTO",
                "Only Animated Scale",
                "Only write scale curves when the sampled pose deviates from identity",
            ),
            (
                "NONE",
                "Skip Scale",
                "Do not write scale curves in sparse animation modes",
            ),
            (
                "ALWAYS",
                "Always Write Scale",
                "Always write scale curves in sparse animation modes",
            ),
        ],
        default="AUTO",
    )

    animation_translation_bones: bpy.props.StringProperty(
        name="Translation Bones",
        description=(
            "Comma-separated bone names that should keep location keys in sparse "
            "animation modes. Leave empty to use root/hips/master heuristics."
        ),
        default="",
    )

    anim_flip_x: bpy.props.BoolProperty(name="Flip X Translation")
    anim_flip_y: bpy.props.BoolProperty(name="Flip Y Translation")
    anim_flip_z: bpy.props.BoolProperty(name="Flip Z Translation")
    anim_flip_quat_x: bpy.props.BoolProperty(name="Flip Quat X")
    anim_flip_quat_y: bpy.props.BoolProperty(name="Flip Quat Y")
    anim_flip_quat_z: bpy.props.BoolProperty(name="Flip Quat Z")
    anim_flip_quat_w: bpy.props.BoolProperty(name="Flip Quat W")
    anim_swap_yz: bpy.props.BoolProperty(name="Swap Y/Z Axes")

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context: bpy.types.Context):
        filepath = Path(self.filepath)
        if filepath.suffix.lower() not in SUPPORTED_EXTENSIONS:
            self.report({"ERROR"}, f"Unsupported extension: {filepath.suffix}")
            return {"CANCELLED"}

        if filepath.suffix.lower() == ".pak" and self.pak_entries:
            if 0 <= self.pak_active_index < len(self.pak_entries):
                active_item = self.pak_entries[self.pak_active_index]
                self.archive_entry = active_item.path or active_item.name

        pak_profile = self.pak_profile if filepath.suffix.lower() == ".pak" else None
        pak_platform = self.pak_platform if filepath.suffix.lower() == ".pak" else None

        target_ext = Path(self.archive_entry or filepath.name).suffix.lower()
        if target_ext == ".igz":
            self._apply_igz_settings()
            try:
                igz_bytes = load_igz_bytes(
                    filepath,
                    entry=self.archive_entry or None,
                    pak_profile=pak_profile,
                    pak_platform=pak_platform,
                )
                self._import_igz_blob(igz_bytes)
            except Exception as exc:  # pragma: no cover - Blender reports the error
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

            self.report({"INFO"}, f"Imported {filepath.name}")
            return {"FINISHED"}

        try:
            pack = load_from_path(
                filepath,
                entry=self.archive_entry or None,
                pak_profile=pak_profile,
                pak_platform=pak_platform,
            )
        except Exception as exc:  # pragma: no cover - Blender reports the error
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        addon_key = __package__.split(".")[0]
        addon_entry = context.preferences.addons.get(addon_key)
        if addon_entry:
            prefs = addon_entry.preferences
        else:
            class _FallbackPrefs:
                scale = 1.0
                forward_axis = "Y"
                up_axis = "Z"

            prefs = _FallbackPrefs()
        axis_mat: Matrix = axis_conversion(
            # Havok skeletons in this pipeline are Y-up with Z-forward.
            # Convert to user-selected Blender axes (default: Y-forward, Z-up).
            from_forward="Z",
            from_up="Y",
            to_forward=prefs.forward_axis,
            to_up=prefs.up_axis,
        ).to_4x4()

        armature_obj = (
            context.active_object
            if context.active_object and context.active_object.type == "ARMATURE"
            else self._get_selected_armature(context)
        )

        # If the source contains a skeleton and the user asked to import it,
        # always create a new armature object from that skeleton definition.
        # Existing scene armatures are only reused for animation-only files.
        if self.import_skeleton and pack.skeleton:
            armature_obj = self._build_armature(context, pack, prefs.scale, axis_mat)

        if self.import_meshes and pack.meshes:
            self._build_meshes(context, pack, prefs.scale, axis_mat, armature_obj)

        selected_animations = self._resolve_animations(pack)
        if self._should_import_animation(target_ext, selected_animations, armature_obj):
            animation_target = self._select_animation_armature(
                context, pack, armature_obj
            )
            if animation_target is None:
                self.report(
                    {"WARNING"},
                    "Animation data found but no armature was available to bind tracks",
                )
            else:
                self._build_animations(
                    context,
                    pack,
                    selected_animations,
                    animation_target,
                    prefs.scale * self.animation_scale,
                    axis_mat,
                )

        self.report({"INFO"}, f"Imported {filepath.name}")
        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "import_meshes")
        layout.prop(self, "import_skeleton")
        target_ext = Path(self.archive_entry or self.filepath).suffix.lower()
        layout.prop(self, "archive_entry")
        if target_ext in {".hka", ".hkx"}:
            layout.separator()
            layout.label(text="Animation options:")
            layout.prop(self, "animation_scale")
            layout.prop(self, "animation_import_mode")
            if self.animation_import_mode != "BAKED_DENSE_ACTION":
                layout.prop(self, "animation_scale_mode")
                layout.prop(self, "animation_translation_bones")

            row = layout.row()
            row.prop(self, "anim_swap_yz")

            col = layout.column(align=True)
            col.label(text="Flip Translation:")
            row = col.row(align=True)
            row.prop(self, "anim_flip_x", text="X")
            row.prop(self, "anim_flip_y", text="Y")
            row.prop(self, "anim_flip_z", text="Z")

            col = layout.column(align=True)
            col.label(text="Flip Quaternion:")
            row = col.row(align=True)
            row.prop(self, "anim_flip_quat_x", text="X")
            row.prop(self, "anim_flip_quat_y", text="Y")
            row.prop(self, "anim_flip_quat_z", text="Z")
            row.prop(self, "anim_flip_quat_w", text="W")

            layout.prop(self, "animation_index")
        if target_ext == ".igz":
            layout.use_property_split = True
            layout.separator()
            layout.label(text="IGZ options:")
            layout.prop(self, "igz_build_meshes")
            layout.prop(self, "igz_build_faces")
            layout.prop(self, "igz_build_bones")
            layout.prop(self, "igz_allow_wii")
            layout.prop(self, "igz_model_threshold")
            layout.prop(self, "igz_first_object_offset")
        if self.filepath.lower().endswith(".pak"):
            layout.prop(self, "pak_profile")
            layout.prop(self, "pak_platform")
            layout.prop(self, "archive_entry")

    @staticmethod
    def _compose_transform(translation: Vector, rotation: mathutils.Quaternion, scale_vec: Vector) -> Matrix:
        matrix = (
            Matrix.Translation(translation)
            @ rotation.to_matrix().to_4x4()
            @ Matrix.Scale(scale_vec[0], 4, (1, 0, 0))
            @ Matrix.Scale(scale_vec[1], 4, (0, 1, 0))
            @ Matrix.Scale(scale_vec[2], 4, (0, 0, 1))
        )
        handedness_flip = Matrix.Scale(-1.0, 4, (1.0, 0.0, 0.0))
        return handedness_flip @ matrix @ handedness_flip

    @staticmethod
    def _is_helper_bone_name(name: str) -> bool:
        lower = name.lower()
        return lower.startswith("z_dummy_") or "target" in lower

    @staticmethod
    def _parse_name_set(raw_value: str) -> Set[str]:
        return {
            token.strip().lower()
            for token in raw_value.replace(";", ",").split(",")
            if token.strip()
        }

    @staticmethod
    def _looks_like_motion_bone(name: str) -> bool:
        normalized = "".join(ch for ch in name.lower() if ch.isalnum())
        return any(
            token in normalized for token in ("root", "master", "hips", "pelvis", "cog")
        )

    def _resolve_translation_bones(
        self,
        animated_names: List[str],
        armature_by_name: Dict[str, bpy.types.Bone],
    ) -> Set[str]:
        explicit_names = self._parse_name_set(self.animation_translation_bones)
        if explicit_names:
            return {
                bone_name
                for bone_name in animated_names
                if bone_name.lower() in explicit_names
            }

        translation_bones: Set[str] = set()
        for bone_name in animated_names:
            bone = armature_by_name.get(bone_name)
            if bone is not None and bone.parent is None:
                translation_bones.add(bone_name)
                continue
            if self._looks_like_motion_bone(bone_name):
                translation_bones.add(bone_name)
        return translation_bones

    @staticmethod
    def _build_sample_frames(
        context: bpy.types.Context, duration: float, frame_count: int
    ) -> List[float]:
        if frame_count <= 0:
            return []
        if frame_count == 1 or duration <= 0:
            return [0.0] * frame_count

        fps = context.scene.render.fps / max(context.scene.render.fps_base, 1e-8)
        seconds_per_sample = duration / max(frame_count - 1, 1)
        return [sample_idx * seconds_per_sample * fps for sample_idx in range(frame_count)]

    @staticmethod
    def _canonicalize_quaternion(
        rotation: mathutils.Quaternion,
        previous: Optional[mathutils.Quaternion],
    ) -> mathutils.Quaternion:
        quat = rotation.copy()
        try:
            quat.normalize()
        except Exception:
            pass
        if previous is not None and quat.dot(previous) < 0.0:
            quat = mathutils.Quaternion((-quat.w, -quat.x, -quat.y, -quat.z))
        return quat

    @staticmethod
    def _values_match(a, b, tolerance: float = 1e-5) -> bool:
        try:
            return all(abs(float(a[idx]) - float(b[idx])) <= tolerance for idx in range(len(a)))
        except Exception:
            return False

    @classmethod
    def _select_sparse_key_indices(
        cls,
        values: List[object],
        default_value,
        *,
        include_default: bool = False,
        tolerance: float = 1e-5,
    ) -> List[int]:
        if not values:
            return []

        if not include_default and all(
            cls._values_match(value, default_value, tolerance) for value in values
        ):
            return []

        last_index = len(values) - 1
        if last_index <= 0:
            return [0]

        selected = [0]
        for idx in range(1, last_index):
            if cls._values_match(values[idx], values[idx - 1], tolerance) and cls._values_match(
                values[idx], values[idx + 1], tolerance
            ):
                continue
            selected.append(idx)

        if last_index not in selected:
            selected.append(last_index)
        return selected

    @staticmethod
    def _insert_channel_keys(
        fcurves_collection,
        data_path: str,
        sample_frames: List[float],
        values: List[object],
        component_count: int,
    ) -> int:
        if not sample_frames or not values:
            return 0

        curves = [fcurves_collection.new(data_path, index=axis) for axis in range(component_count)]
        inserted = 0
        for sample_frame, value in zip(sample_frames, values):
            for axis, curve in enumerate(curves):
                curve.keyframe_points.insert(
                    sample_frame, float(value[axis]), options={"FAST"}
                ).interpolation = "LINEAR"
                inserted += 1
        return inserted

    @staticmethod
    def _ensure_action_fcurves(
        action: bpy.types.Action, animation_data: bpy.types.AnimData
    ):
        if hasattr(action, "fcurves"):
            return action.fcurves

        slot = action.slots.new("OBJECT", "Slot")
        layer = action.layers.new("Layer")
        strip = layer.strips.new(type="KEYFRAME")
        bag = strip.channelbags.new(slot)

        if hasattr(animation_data, "action_slot_handle"):
            animation_data.action_slot_handle = slot.handle

        return bag.fcurves

    def _create_action(
        self, armature_obj: bpy.types.Object, action_name: str
    ) -> tuple[bpy.types.Action, object]:
        animation_data = armature_obj.animation_data_create()
        action = bpy.data.actions.new(action_name)
        action.use_fake_user = True
        animation_data.action = action
        return action, self._ensure_action_fcurves(action, animation_data)

    @staticmethod
    def _push_action_to_nla(
        armature_obj: bpy.types.Object,
        action: bpy.types.Action,
        blend_hint: str,
    ) -> None:
        animation_data = armature_obj.animation_data_create()
        track = animation_data.nla_tracks.new()
        track.name = action.name

        action_start = float(action.frame_range[0]) if action.frame_range else 0.0
        action_end = float(action.frame_range[1]) if action.frame_range else action_start + 1.0
        strip_start = int(round(action_start))
        strip = track.strips.new(action.name, strip_start, action)
        strip.name = action.name
        strip.action_frame_start = action_start
        strip.action_frame_end = action_end
        strip.frame_start = strip_start
        strip.frame_end = max(strip_start + (action_end - action_start), strip_start + 1.0)

        if strip.frame_end <= strip.frame_start:
            strip.frame_end = strip.frame_start + 1.0

        if blend_hint == "ADDITIVE":
            try:
                strip.blend_type = "ADD"
            except Exception:
                pass

    def _finalize_action_import(
        self,
        armature_obj: bpy.types.Object,
        action: bpy.types.Action,
        import_mode: str,
        animation: parsers.HavokAnimation,
    ) -> None:
        animation_data = armature_obj.animation_data_create()
        if import_mode == "ACTION_PUSH_NLA":
            self._push_action_to_nla(armature_obj, action, animation.blend_hint)
            animation_data.action = None
            return
        animation_data.action = action

    def _build_armature(
        self,
        context: bpy.types.Context,
        pack: HavokPack,
        scale: float,
        axis_mat: Matrix,
    ) -> bpy.types.Object:
        assert pack.skeleton
        skel = pack.skeleton
        armature = bpy.data.armatures.new(skel.name)
        armature_obj = bpy.data.objects.new(skel.name, armature)
        try:
            armature.display_type = "OCTAHEDRAL"
        except Exception:
            pass

        collection = _get_or_create_collection(context, "Havok Imports")
        collection.objects.link(armature_obj)
        bpy.context.view_layer.objects.active = armature_obj

        global_transforms: List[Matrix] = []
        children: Dict[int, List[int]] = {idx: [] for idx in range(len(skel.bones))}

        for idx, bone in enumerate(skel.bones):
            local_mat = self._compose_transform(bone.translation, bone.rotation, bone.scale)

            if bone.parent >= 0 and bone.parent < len(global_transforms):
                global_mat = global_transforms[bone.parent] @ local_mat
                children[bone.parent].append(idx)
            else:
                global_mat = axis_mat @ local_mat

            global_transforms.append(global_mat)

        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones: List[bpy.types.EditBone] = []
        for bone in skel.bones:
            edit_bones.append(armature.edit_bones.new(bone.name))

        for idx, bone in enumerate(skel.bones):
            if bone.parent >= 0 and bone.parent < len(skel.bones):
                edit_bones[idx].parent = edit_bones[bone.parent]

        min_len = max(0.02 * scale, 1e-4)
        default_len = max(0.10 * scale, min_len)

        for idx, bone in enumerate(skel.bones):
            edit_bone = edit_bones[idx]
            global_mat = global_transforms[idx]
            head = global_mat.to_translation() * scale
            rot = global_mat.to_quaternion()

            child_dists = []
            child_vectors = []
            for child_idx in children.get(idx, []):
                vec = (
                    global_transforms[child_idx].to_translation()
                    - global_mat.to_translation()
                ) * scale
                dist = vec.length
                if dist > 1e-6:
                    child_dists.append(dist)
                    child_vectors.append(vec)

            if child_dists:
                useful = [d for d in child_dists if d >= (min_len * 1.5)]
                bone_len = max(min(useful if useful else child_dists), min_len)
            elif bone.parent >= 0:
                parent_dist = (
                    global_mat.to_translation()
                    - global_transforms[bone.parent].to_translation()
                ).length * scale
                bone_len = max(parent_dist * 0.5, min_len)
            else:
                bone_len = default_len

            child_dir = None
            if child_vectors:
                useful_vecs = [v for v in child_vectors if v.length >= (min_len * 1.5)]
                candidate_vecs = useful_vecs if useful_vecs else child_vectors
                chosen_vec = min(candidate_vecs, key=lambda v: abs(v.length - bone_len))
                if chosen_vec.length > 1e-8:
                    child_dir = chosen_vec.normalized()

            if child_dir is None and bone.parent >= 0:
                parent_head = global_transforms[bone.parent].to_translation() * scale
                vec = head - parent_head
                if vec.length > 1e-8:
                    child_dir = vec.normalized()

            if child_dir is None:
                child_dir = (rot @ Vector((0.0, 1.0, 0.0))).normalized()

            edit_bone.head = head
            edit_bone.tail = head + child_dir * bone_len

            # Preserve roll from Havok orientation, not only head/tail direction.
            up_axis = rot @ Vector((0.0, 0.0, 1.0))
            if up_axis.length_squared > 1e-8:
                try:
                    edit_bone.align_roll(up_axis)
                except Exception:
                    pass

        bpy.ops.object.mode_set(mode="OBJECT")

        # Cache Havok metadata on Blender bones so animation-only HKX files
        # (without embedded skeleton) can still map tracks correctly.
        for idx, bone in enumerate(skel.bones):
            dst_bone = armature_obj.data.bones.get(bone.name)
            if not dst_bone:
                continue
            if self._is_helper_bone_name(bone.name):
                dst_bone.use_deform = False
            dst_bone["havok_index"] = idx
            dst_bone["havok_rest_t"] = [
                float(bone.translation[0]),
                float(bone.translation[1]),
                float(bone.translation[2]),
            ]
            dst_bone["havok_rest_r"] = [
                float(bone.rotation.w),
                float(bone.rotation.x),
                float(bone.rotation.y),
                float(bone.rotation.z),
            ]
            dst_bone["havok_rest_s"] = [
                float(bone.scale[0]),
                float(bone.scale[1]),
                float(bone.scale[2]),
            ]

        return armature_obj

    def _load_pak_entries(self) -> None:
        previous_path = self.archive_entry

        self.pak_entries.clear()
        if not self.filepath:
            return
        try:
            entries = parsers.enumerate_pak_entries(
                Path(self.filepath), self.pak_profile, self.pak_platform
            )
        except Exception:
            return

        tree = _build_pak_tree(entries)
        for node in tree:
            item = self.pak_entries.add()
            item.name = node["name"]
            item.path = node["path"]
            item.size = node.get("size", 0)
            item.mode = node.get("mode", "")
            item.is_dir = node.get("is_dir", False)
            item.depth = node.get("depth", 0)

        preferred = next(
            (n for n in tree if n.get("path") == previous_path and not n["is_dir"]),
            None,
        )
        if preferred:
            self.archive_entry = preferred["path"]
            self.pak_active_index = tree.index(preferred)
        else:
            first_leaf = next((n for n in tree if not n["is_dir"]), None)
            if first_leaf:
                self.archive_entry = first_leaf["path"]
                self.pak_active_index = tree.index(first_leaf)
            else:
                self.archive_entry = ""
                self.pak_active_index = 0

        self.last_pak_path = self.filepath
        self.last_pak_profile = self.pak_profile
        self.last_pak_platform = self.pak_platform

        self._refresh_animation_metadata()

    def _build_animations(
        self,
        context: bpy.types.Context,
        pack: HavokPack,
        animations: List[parsers.HavokAnimation],
        armature_obj: Optional[bpy.types.Object],
        scale: float,
        axis_mat: Matrix,
    ) -> None:
        if armature_obj is None:
            return

        identity = Matrix.Identity(4)
        armature_data = armature_obj.data
        armature_bones = armature_data.bones
        armature_by_name: Dict[str, bpy.types.Bone] = {b.name: b for b in armature_bones}

        # Build Havok skeleton map (index -> {name,parent,rest_local}).
        havok_nodes: Dict[int, Dict[str, object]] = {}
        name_to_havok_index: Dict[str, int] = {}
        roots_use_axis_mat = True

        if pack.skeleton:
            for idx, bone in enumerate(pack.skeleton.bones):
                havok_nodes[idx] = {
                    "name": bone.name,
                    "parent": bone.parent,
                    "rest_local": self._compose_transform(
                        bone.translation, bone.rotation, bone.scale
                    ),
                }
                name_to_havok_index[bone.name] = idx
        else:
            def _to_float_seq(value, expected_len: int) -> Optional[List[float]]:
                try:
                    if value is None or len(value) != expected_len:
                        return None
                    return [float(value[i]) for i in range(expected_len)]
                except Exception:
                    return None

            for b in armature_bones:
                idx_val = b.get("havok_index")
                t = b.get("havok_rest_t")
                r = b.get("havok_rest_r")
                s = b.get("havok_rest_s")
                t_vals = _to_float_seq(t, 3)
                r_vals = _to_float_seq(r, 4)
                s_vals = _to_float_seq(s, 3)
                if (
                    isinstance(idx_val, (int, float))
                    and t_vals is not None
                    and r_vals is not None
                    and s_vals is not None
                ):
                    idx = int(idx_val)
                    parent_idx = -1
                    if b.parent:
                        p_idx_val = b.parent.get("havok_index")
                        if isinstance(p_idx_val, (int, float)):
                            parent_idx = int(p_idx_val)
                    havok_nodes[idx] = {
                        "name": b.name,
                        "parent": parent_idx,
                        "rest_local": self._compose_transform(
                            Vector((t_vals[0], t_vals[1], t_vals[2])),
                            mathutils.Quaternion(
                                (r_vals[0], r_vals[1], r_vals[2], r_vals[3])
                            ),
                            Vector((s_vals[0], s_vals[1], s_vals[2])),
                        ),
                    }
                    name_to_havok_index[b.name] = idx

        has_annotation_names = any(
            bool(name)
            for anim in animations
            for name in getattr(anim, "annotation_tracks", [])
        )

        # Last-resort fallback if this armature was not created by this importer.
        # For animation-only files without annotation names, index-only mapping is
        # ambiguous, so require importer tags (havok_index) instead.
        if not havok_nodes:
            roots_use_axis_mat = False
            if not pack.skeleton and not has_annotation_names:
                self.report(
                    {"WARNING"},
                    "Animation-only file has no skeleton names; import/apply it on a Havok-imported armature first",
                )
                return
            for idx, b in enumerate(armature_bones):
                parent_idx = idx - 1
                if b.parent and b.parent.name in name_to_havok_index:
                    parent_idx = name_to_havok_index[b.parent.name]
                elif not b.parent:
                    parent_idx = -1
                if b.parent:
                    rest_local = b.parent.matrix_local.inverted() @ b.matrix_local
                else:
                    rest_local = b.matrix_local
                havok_nodes[idx] = {
                    "name": b.name,
                    "parent": parent_idx,
                    "rest_local": rest_local.copy(),
                }
                name_to_havok_index[b.name] = idx

        # Ensure parent indices are valid.
        valid_indices = set(havok_nodes.keys())
        for idx, node in havok_nodes.items():
            p = int(node["parent"]) if isinstance(node["parent"], int) else -1
            if p not in valid_indices:
                node["parent"] = -1

        # Rest transforms in Blender armature space.
        blender_rest_globals: Dict[str, Matrix] = {}
        blender_rest_locals: Dict[str, Matrix] = {}
        for b in armature_bones:
            blender_rest_globals[b.name] = b.matrix_local.copy()
            if b.parent:
                blender_rest_locals[b.name] = b.parent.matrix_local.inverted() @ b.matrix_local
            else:
                blender_rest_locals[b.name] = b.matrix_local.copy()

        # Recursive global builder to avoid assumptions about index ordering.
        def build_globals(local_mats: Dict[int, Matrix]) -> Dict[int, Matrix]:
            cache: Dict[int, Matrix] = {}

            def resolve(idx: int) -> Matrix:
                if idx in cache:
                    return cache[idx]
                node = havok_nodes[idx]
                local = local_mats.get(idx, identity)
                parent_idx = int(node["parent"]) if isinstance(node["parent"], int) else -1
                if parent_idx in havok_nodes:
                    mat = resolve(parent_idx) @ local
                else:
                    mat = axis_mat @ local if roots_use_axis_mat else local
                cache[idx] = mat
                return mat

            for nidx in havok_nodes.keys():
                resolve(nidx)
            return cache

        # Havok rest global transforms.
        havok_rest_locals_by_index: Dict[int, Matrix] = {
            idx: havok_nodes[idx]["rest_local"].copy() for idx in havok_nodes.keys()
        }
        havok_rest_globals = build_globals(havok_rest_locals_by_index)

        # Global rest mapping from Havok -> Blender.
        global_fixes: Dict[int, Matrix] = {}
        for idx, node in havok_nodes.items():
            name = str(node["name"])
            h_rest_global = havok_rest_globals.get(idx, identity)
            b_rest_global = blender_rest_globals.get(name)
            if b_rest_global is not None:
                try:
                    global_fixes[idx] = h_rest_global.inverted() @ b_rest_global
                except Exception:
                    global_fixes[idx] = identity
            else:
                global_fixes[idx] = identity

        imported_actions = 0
        skipped_unmatched = 0

        for animation in animations:

            # Resolve track -> Havok bone index map once.
            track_to_havok: Dict[int, int] = {}
            for track_idx, track in enumerate(animation.tracks):
                if not track:
                    continue

                bone_name = None
                bone_idx = -1

                # Try to find bone by annotation track name first
                if track_idx < len(animation.annotation_tracks):
                    annot_name = animation.annotation_tracks[track_idx]
                    if annot_name and annot_name in name_to_havok_index:
                        bone_name = annot_name
                        bone_idx = name_to_havok_index[annot_name]

                # Fallback to binding indices
                if bone_idx < 0:
                    candidate_idx = (
                        animation.track_to_bone[track_idx]
                        if track_idx < len(animation.track_to_bone)
                        else track_idx
                    )
                    if candidate_idx in havok_nodes:
                        bone_idx = candidate_idx
                        bone_name = str(havok_nodes[candidate_idx]["name"])

                if bone_idx < 0 or not bone_name:
                    continue
                if self._is_helper_bone_name(bone_name):
                    continue
                track_to_havok[track_idx] = bone_idx

            if not track_to_havok:
                skipped_unmatched += 1
                continue

            animated_indices = sorted(set(track_to_havok.values()))
            animated_names = [
                str(havok_nodes[idx]["name"])
                for idx in animated_indices
                if str(havok_nodes[idx]["name"]) in armature_by_name
            ]

            if not animated_names:
                skipped_unmatched += 1
                continue

            frame_count = max((len(t) for t in animation.tracks if t), default=0)
            if frame_count <= 0:
                skipped_unmatched += 1
                continue

            sample_frames = self._build_sample_frames(context, animation.duration, frame_count)
            bone_samples: Dict[str, Dict[str, list]] = {
                bone_name: {
                    "location": [],
                    "rotation_quaternion": [],
                    "scale": [],
                }
                for bone_name in animated_names
            }

            for frame_idx in range(frame_count):
                # Start from rest for bones without keyed tracks.
                havok_anim_locals: Dict[int, Matrix] = {
                    idx: mat.copy() for idx, mat in havok_rest_locals_by_index.items()
                }

                for track_idx, bone_idx in track_to_havok.items():
                    track = animation.tracks[track_idx]
                    if not track:
                        continue
                    sample = track[min(frame_idx, len(track) - 1)]
                    trans, quat, scale_vec = sample

                    t_x, t_y, t_z = trans
                    q_w, q_x, q_y, q_z = quat.w, quat.x, quat.y, quat.z

                    if self.anim_swap_yz:
                        t_y, t_z = t_z, t_y
                        q_y, q_z = q_z, q_y

                    if self.anim_flip_x:
                        t_x = -t_x
                    if self.anim_flip_y:
                        t_y = -t_y
                    if self.anim_flip_z:
                        t_z = -t_z

                    if self.anim_flip_quat_x:
                        q_x = -q_x
                    if self.anim_flip_quat_y:
                        q_y = -q_y
                    if self.anim_flip_quat_z:
                        q_z = -q_z
                    if self.anim_flip_quat_w:
                        q_w = -q_w

                    havok_anim_locals[bone_idx] = self._compose_transform(
                        Vector((t_x, t_y, t_z)) * scale,
                        mathutils.Quaternion((q_w, q_x, q_y, q_z)),
                        Vector(scale_vec),
                    )

                havok_anim_globals = build_globals(havok_anim_locals)

                # Convert to target Blender armature-space globals.
                blender_target_globals: Dict[str, Matrix] = {}
                for idx, node in havok_nodes.items():
                    name = str(node["name"])
                    blender_target_globals[name] = (
                        havok_anim_globals.get(idx, identity) @ global_fixes.get(idx, identity)
                    )

                for bone_name in animated_names:
                    bone_data = armature_by_name.get(bone_name)
                    target_global = blender_target_globals.get(bone_name)
                    if bone_data is None or target_global is None:
                        continue

                    if bone_data.parent:
                        parent_name = bone_data.parent.name
                        parent_target = blender_target_globals.get(parent_name)
                        rest_local = blender_rest_locals.get(bone_name, identity)
                        if parent_target is not None:
                            matrix_basis = (
                                rest_local.inverted()
                                @ parent_target.inverted()
                                @ target_global
                            )
                        else:
                            matrix_basis = (
                                blender_rest_globals.get(bone_name, identity).inverted()
                                @ target_global
                            )
                    else:
                        matrix_basis = (
                            blender_rest_globals.get(bone_name, identity).inverted()
                            @ target_global
                        )

                    loc, rot, sca = matrix_basis.decompose()
                    bone_sample = bone_samples[bone_name]
                    previous_rot = (
                        bone_sample["rotation_quaternion"][-1]
                        if bone_sample["rotation_quaternion"]
                        else None
                    )
                    rot = self._canonicalize_quaternion(rot, previous_rot)
                    bone_sample["location"].append(loc.copy())
                    bone_sample["rotation_quaternion"].append(rot.copy())
                    bone_sample["scale"].append(sca.copy())

            action_name = animation.name or f"Animation_{imported_actions}"
            action, fcurves_collection = self._create_action(armature_obj, action_name)

            inserted_keyframes = 0
            import_mode = self.animation_import_mode
            dense_mode = import_mode == "BAKED_DENSE_ACTION"
            translation_bones = self._resolve_translation_bones(
                animated_names, armature_by_name
            )
            loc_default = Vector((0.0, 0.0, 0.0))
            rot_default = mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))
            scale_default = Vector((1.0, 1.0, 1.0))

            for bone_name in animated_names:
                pose_bone = armature_obj.pose.bones.get(bone_name)
                if not pose_bone:
                    continue

                pose_bone.rotation_mode = "QUATERNION"
                bone_sample = bone_samples.get(bone_name)
                if not bone_sample:
                    continue

                data_path_loc = pose_bone.path_from_id("location")
                data_path_rot = pose_bone.path_from_id("rotation_quaternion")
                data_path_scale = pose_bone.path_from_id("scale")

                if dense_mode:
                    inserted_keyframes += self._insert_channel_keys(
                        fcurves_collection,
                        data_path_loc,
                        sample_frames,
                        bone_sample["location"],
                        3,
                    )
                    inserted_keyframes += self._insert_channel_keys(
                        fcurves_collection,
                        data_path_rot,
                        sample_frames,
                        bone_sample["rotation_quaternion"],
                        4,
                    )
                    inserted_keyframes += self._insert_channel_keys(
                        fcurves_collection,
                        data_path_scale,
                        sample_frames,
                        bone_sample["scale"],
                        3,
                    )
                    continue

                if bone_name in translation_bones:
                    loc_indices = self._select_sparse_key_indices(
                        bone_sample["location"], loc_default
                    )
                    if loc_indices:
                        inserted_keyframes += self._insert_channel_keys(
                            fcurves_collection,
                            data_path_loc,
                            [sample_frames[idx] for idx in loc_indices],
                            [bone_sample["location"][idx] for idx in loc_indices],
                            3,
                        )

                rot_indices = self._select_sparse_key_indices(
                    bone_sample["rotation_quaternion"], rot_default
                )
                if rot_indices:
                    inserted_keyframes += self._insert_channel_keys(
                        fcurves_collection,
                        data_path_rot,
                        [sample_frames[idx] for idx in rot_indices],
                        [bone_sample["rotation_quaternion"][idx] for idx in rot_indices],
                        4,
                    )

                if self.animation_scale_mode != "NONE":
                    scale_indices = self._select_sparse_key_indices(
                        bone_sample["scale"],
                        scale_default,
                        include_default=self.animation_scale_mode == "ALWAYS",
                    )
                    if scale_indices:
                        inserted_keyframes += self._insert_channel_keys(
                            fcurves_collection,
                            data_path_scale,
                            [sample_frames[idx] for idx in scale_indices],
                            [bone_sample["scale"][idx] for idx in scale_indices],
                            3,
                        )

            if inserted_keyframes <= 0:
                armature_obj.animation_data.action = None
                bpy.data.actions.remove(action)
                skipped_unmatched += 1
                continue

            self._finalize_action_import(armature_obj, action, import_mode, animation)
            imported_actions += 1

        if imported_actions == 0 and animations:
            self.report(
                {"WARNING"},
                "No animation tracks matched the target armature; select/import the correct skeleton first",
            )
        elif skipped_unmatched > 0:
            self.report(
                {"INFO"},
                f"Imported {imported_actions} action(s); skipped {skipped_unmatched} unmatched animation(s)",
            )

    def _get_selected_armature(
        self, context: bpy.types.Context
    ) -> Optional[bpy.types.Object]:
        for obj in context.selected_objects:
            if obj.type == "ARMATURE":
                return obj
        scene_armatures = [obj for obj in context.scene.objects if obj.type == "ARMATURE"]
        if len(scene_armatures) == 1:
            return scene_armatures[0]
        return None

    @staticmethod
    def _count_havok_tagged_bones(armature_obj: Optional[bpy.types.Object]) -> int:
        if armature_obj is None or armature_obj.type != "ARMATURE":
            return 0
        return sum(1 for b in armature_obj.data.bones if "havok_index" in b)

    @staticmethod
    def _count_skeleton_name_overlap(
        armature_obj: Optional[bpy.types.Object], skeleton: Optional[parsers.HavokSkeleton]
    ) -> int:
        if (
            armature_obj is None
            or armature_obj.type != "ARMATURE"
            or skeleton is None
        ):
            return 0
        arm_names = {b.name for b in armature_obj.data.bones}
        return sum(1 for bone in skeleton.bones if bone.name in arm_names)

    def _select_animation_armature(
        self,
        context: bpy.types.Context,
        pack: HavokPack,
        preferred: Optional[bpy.types.Object],
    ) -> Optional[bpy.types.Object]:
        scene_armatures = [obj for obj in context.scene.objects if obj.type == "ARMATURE"]
        if not scene_armatures:
            return preferred

        if pack.skeleton is not None:
            def score(obj: bpy.types.Object) -> tuple[int, int, int]:
                return (
                    self._count_skeleton_name_overlap(obj, pack.skeleton),
                    self._count_havok_tagged_bones(obj),
                    len(obj.data.bones),
                )
        else:
            def score(obj: bpy.types.Object) -> tuple[int, int]:
                return (
                    self._count_havok_tagged_bones(obj),
                    len(obj.data.bones),
                )

        best = max(scene_armatures, key=score)
        best_score = score(best)
        pref_score = score(preferred) if preferred and preferred.type == "ARMATURE" else None

        if pack.skeleton is not None:
            # Prefer the armature with the strongest skeleton-name match.
            if best_score[0] > 0:
                if pref_score and pref_score[0] > 0 and pref_score >= best_score:
                    return preferred
                return best
            return preferred

        # Animation-only files: prefer importer-tagged armatures (havok_index).
        if pref_score and pref_score[0] > 0:
            return preferred
        if best_score[0] > 0:
            return best
        return preferred

    def _should_import_animation(
        self,
        target_ext: str,
        animations: List[parsers.HavokAnimation],
        armature_obj: Optional[bpy.types.Object],
    ) -> bool:
        if not animations:
            return False
        if target_ext == ".hka":
            return armature_obj is not None
        return True

    def _resolve_animations(self, pack: HavokPack) -> List[parsers.HavokAnimation]:
        # Use animation_active_index if available and valid
        if self.animation_entries and 0 <= self.animation_active_index < len(
            self.animation_entries
        ):
            entry = self.animation_entries[self.animation_active_index]
            if entry.index == -1:
                return list(pack.animations)
            if 0 <= entry.index < len(pack.animations):
                return [pack.animations[entry.index]]

        # Fallback to legacy animation_index
        if self.animation_index < 0:
            return list(pack.animations)

        if self.animation_index >= len(pack.animations):
            self.report(
                {"WARNING"},
                f"Animation index {self.animation_index} is out of range; no animations imported",
            )
            return []

        return [pack.animations[self.animation_index]]

    def _refresh_animation_metadata(self) -> None:
        filepath = Path(self.filepath) if self.filepath else None
        if filepath is None or not filepath.suffix:
            return

        target_ext = Path(self.archive_entry or filepath.name).suffix.lower()
        if target_ext not in {".hka", ".hkx"}:
            if self.animation_metadata_key:
                self.animation_metadata_key = ""
                self.animation_count = 0
                self.animation_names = ""
            return

        key = "|".join(
            [
                str(filepath),
                self.archive_entry,
                self.pak_profile,
                self.pak_platform,
            ]
        )
        if key == self.animation_metadata_key:
            return

        self.animation_metadata_key = key
        self.animation_count = 0
        self.animation_names = ""

        try:
            pack = load_from_path(
                filepath,
                entry=self.archive_entry or None,
                pak_profile=(
                    self.pak_profile if filepath.suffix.lower() == ".pak" else None
                ),
                pak_platform=(
                    self.pak_platform if filepath.suffix.lower() == ".pak" else None
                ),
            )
        except Exception:
            return

        self.animation_count = len(pack.animations)
        self.animation_names = "\n".join(
            anim.name or f"Animation {idx}" for idx, anim in enumerate(pack.animations)
        )

        self.animation_entries.clear()

        # Add "All Animations" option
        item = self.animation_entries.add()
        item.name = "All Animations"
        item.index = -1
        item.duration = 0.0
        item.num_tracks = 0

        for idx, anim in enumerate(pack.animations):
            item = self.animation_entries.add()
            item.name = anim.name or f"Animation {idx}"
            item.index = idx
            item.duration = anim.duration
            item.num_tracks = len(anim.tracks)

        if self.animation_index >= self.animation_count and self.animation_count:
            self.animation_index = 0

    def _apply_igz_settings(self) -> None:
        igz_constants.dBuildMeshes = self.igz_build_meshes
        igz_constants.dBuildBones = self.igz_build_bones
        igz_constants.dBuildFaces = self.igz_build_faces
        igz_constants.dAllowWii = self.igz_allow_wii
        igz_constants.dModelThreshold = self.igz_model_threshold
        igz_constants.dFirstObjectOffset = self.igz_first_object_offset

    def _import_igz_blob(self, data: bytes):
        # Mirror the io_scene_igz import path by letting its parser build Blender
        # objects directly from the binary IGZ stream.
        from ..io.igz_port import game_formats as igz_formats

        profile_to_parser: Dict[str, Callable[[bytes], object]] = {
            "IMAGINATORS": igz_formats.sscIgzFile,
            "SSA_WII": igz_formats.ssaIgzFile,
            "SSA_WIIU": igz_formats.ssaIgzFile,
            "SWAP_FORCE": igz_formats.ssfIgzFile,
            "TRAP_TEAM": igz_formats.sttIgzFile,
            "SUPER_CHARGERS": igz_formats.sscIgzFile,
        }

        parser_factory = profile_to_parser.get(self.pak_profile, igz_formats.sscIgzFile)
        parser = parser_factory(data)
        parser.loadFile()
        parser.buildMeshes()

    def _build_meshes(
        self,
        context: bpy.types.Context,
        pack: HavokPack,
        scale: float,
        axis_mat: Matrix,
        armature_obj: Optional[bpy.types.Object],
    ) -> None:
        collection = _get_or_create_collection(context, "Havok Imports")
        for mesh_data in pack.meshes:
            mesh = bpy.data.meshes.new(mesh_data.name)
            transformed_verts = [
                (axis_mat @ (v * scale).to_4d()).to_3d() for v in mesh_data.vertices
            ]
            mesh.from_pydata(transformed_verts, [], mesh_data.faces)
            mesh.update()

            obj = bpy.data.objects.new(mesh_data.name, mesh)
            collection.objects.link(obj)
            if armature_obj:
                obj.parent = armature_obj


def _get_or_create_collection(context: bpy.types.Context, name: str):
    root = context.scene.collection
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    collection = bpy.data.collections.new(name)
    root.children.link(collection)
    return collection


def menu_func_import(self, _context):
    self.layout.operator(
        HAVOK_OT_import.bl_idname, text="Havok (.hkx/.hkt/.hka/.igz/.pak)"
    )
