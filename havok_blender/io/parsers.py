from __future__ import annotations

import ast
import gzip
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

import mathutils

from .binary_parser import BinaryReader, hkxHeader

SUPPORTED_EXTENSIONS = {".hkx", ".txt", ".igz", ".hkt", ".pak", ".hka"}

PAK_PLATFORM_ENDIANNESS = {
    "little": "Little endian (PC / Xbox 360 / Xbox One / Wii U)",
    "big": "Big endian (PS3 / Wii)",
}

_PAK_PROFILES: list = []
PAK_PROFILE_NAMES: tuple = ()


@dataclass
class HavokBone:
    name: str
    parent: int
    translation: "mathutils.Vector" = None
    rotation: "mathutils.Quaternion" = None
    scale: "mathutils.Vector" = None

    def __post_init__(self):
        if self.translation is None:
            self.translation = mathutils.Vector((0, 0, 0))
        if self.rotation is None:
            self.rotation = mathutils.Quaternion((1, 0, 0, 0))
        if self.scale is None:
            self.scale = mathutils.Vector((1, 1, 1))


@dataclass
class HavokSkeleton:
    name: str
    bones: List[HavokBone]


@dataclass
class HavokAnimation:
    name: str
    duration: float
    tracks: List[list]
    track_to_bone: List[int]
    annotation_tracks: List[str]
    blend_hint: str = "NORMAL"


@dataclass
class HavokPack:
    skeleton: Optional[HavokSkeleton]
    animations: List[HavokAnimation]
    meshes: list


@dataclass
class HavokMesh:
    name: str
    vertices: list
    faces: list


@dataclass
class PakEntry:
    name: str
    offset: int
    size: int
    mode: int
    endianness: str
    version: int
    chunk_alignment: int
    size_field: int
    size_endianness: str


def load_from_path(path, entry=None, pak_profile=None, pak_platform=None):
    suffix = path.suffix.lower()
    if suffix == ".pak":
        data = _extract_from_archive(path, entry, pak_profile, pak_platform)
    else:
        data = path.read_bytes()
    return parse_bytes(data, override_name=path.stem)


def load_igz_bytes(path, entry=None, pak_profile=None, pak_platform=None):
    suffix = path.suffix.lower()
    if suffix == ".pak":
        return _extract_from_archive(path, entry, pak_profile, pak_platform)
    return path.read_bytes()


def parse_bytes(data, override_name=None):
    data = _unwrap_bytes(data)
    if data[:4] == b"W\xe0\xe0W":
        return _parse_binary_packfile(data, override_name)
    try:
        root = ET.fromstring(data)
        skeleton = _parse_skeleton(root, override_name)
        animations = _parse_animations(root, skeleton)
        meshes = _parse_meshes(root, override_name)
        return HavokPack(skeleton=skeleton, animations=animations, meshes=meshes)
    except ET.ParseError as exc:
        raise ValueError("Unsupported or corrupt Havok payload") from exc


def _unwrap_bytes(data):
    try:
        if len(data) > 3 and data[0] == 98 and data[1] in (39, 34):
            text = data.decode("utf-8", errors="ignore")
            stripped = text.strip()
            if stripped.startswith(("b'", 'b"')):
                return ast.literal_eval(stripped)
        if len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B:
            return gzip.decompress(data)
    except (ValueError, SyntaxError):
        pass
    return data


def _parse_binary_packfile(data, override_name=None):
    """Parse binary Havok packfile using pure Python implementation.

    KEY FIX: This version processes hkaAnimationContainer variants
    instead of skipping them (which was the bug in the original .pyc).
    """
    reader = BinaryReader(data)
    header = hkxHeader()
    header.load(reader)

    variants = header.get_root_level_container()
    print(f"DEBUG: Root variants ({len(variants)}):")
    for v in variants:
        print(f"  name={v['name']!r}  class={v['class_name']!r}  ptr={v['variant_ptr']}")

    skeleton = None
    animations = []
    meshes = []

    for variant in variants:
        cn = variant["class_name"]

        # KEY FIX: Only process hkaAnimationContainer variants.
        # The original .pyc code had this inverted - it SKIPPED hkaAnimationContainer
        # and tried to process everything else as an animation container, which
        # meant no skeleton/animation data was ever loaded.
        if cn != "hkaAnimationContainer":
            print(f"DEBUG: Skipping variant class '{cn}'")
            continue

        if variant["variant_ptr"] is None:
            print(f"DEBUG: variant_ptr is None for '{cn}', skipping")
            continue

        sid, soff = variant["variant_ptr"]
        container = header.read_hka_animation_container(sid, soff)
        print(f"DEBUG: Animation container parsed: "
              f"skeletons={container['skeletons']}, "
              f"animations={container['animations']}, "
              f"bindings={container['bindings']}")

        # --- Skeletons ---
        skeletons_ptr, skeletons_size = container["skeletons"]
        if skeletons_ptr:
            sid, soff = skeletons_ptr
            ptr_size = header.layout.bytes_in_pointer
            for i in range(skeletons_size):
                skel_ptr = header.read_pointer(sid, soff + i * ptr_size)
                if not skel_ptr:
                    continue
                skel_sid, skel_soff = skel_ptr
                skel_data = header.read_hka_skeleton(skel_sid, skel_soff)

                bones = []
                for b in skel_data["bones"]:
                    bones.append(HavokBone(
                        name=b["name"],
                        parent=b["parent"],
                    ))

                # Fill in reference pose data
                if skel_data.get("ref_poses"):
                    for idx, pose in enumerate(skel_data["ref_poses"]):
                        if idx >= len(bones):
                            break
                        bones[idx].translation = mathutils.Vector(pose["translation"])
                        q = mathutils.Quaternion((
                            pose["rotation"][3],  # w
                            pose["rotation"][0],  # x
                            pose["rotation"][1],  # y
                            pose["rotation"][2],  # z
                        ))
                        bones[idx].rotation = q.conjugated()
                        bones[idx].scale = mathutils.Vector(pose["scale"])

                skeleton = HavokSkeleton(name=skel_data["name"], bones=bones)
                print(f"DEBUG: Loaded skeleton '{skel_data['name']}' with {len(bones)} bones")

        # --- Bindings ---
        bindings_ptr, bindings_size = container["bindings"]
        bindings = []
        if bindings_ptr:
            sid, soff = bindings_ptr
            ptr_size = header.layout.bytes_in_pointer
            for i in range(bindings_size):
                bind_ptr = header.read_pointer(sid, soff + i * ptr_size)
                if not bind_ptr:
                    continue
                bsid, bsoff = bind_ptr
                bindings.append(header.read_hka_animation_binding(bsid, bsoff))

        anim_to_binding = {}
        for b in bindings:
            anim_to_binding[b["animation_ptr"]] = b

        # --- Animations ---
        animations_ptr, animations_size = container["animations"]
        if animations_ptr:
            sid, soff = animations_ptr
            ptr_size = header.layout.bytes_in_pointer
            for i in range(animations_size):
                anim_ptr = header.read_pointer(sid, soff + i * ptr_size)
                if not anim_ptr:
                    continue
                asid, asoff = anim_ptr
                anim_data = header.read_hka_animation(asid, asoff)

                binding = anim_to_binding.get(anim_ptr)
                if binding:
                    track_to_bone = binding["track_to_bone"]
                else:
                    track_to_bone = list(range(len(anim_data["tracks"])))

                blend_hint = "NORMAL"
                if binding and binding.get("blend_hint", 0) == 1:
                    blend_hint = "ADDITIVE"

                converted_tracks = []
                for t in anim_data["tracks"]:
                    track_frames = []
                    for frame in t:
                        trans, rot, scale = frame
                        vec = mathutils.Vector(trans)
                        quat = mathutils.Quaternion((rot[3], rot[0], rot[1], rot[2]))
                        sca = mathutils.Vector(scale)
                        track_frames.append((vec, quat.conjugated(), sca))
                    converted_tracks.append(track_frames)

                animations.append(HavokAnimation(
                    name=anim_data["name"],
                    duration=anim_data["duration"],
                    tracks=converted_tracks,
                    track_to_bone=track_to_bone,
                    annotation_tracks=[],
                    blend_hint=blend_hint,
                ))
                print(f"DEBUG: Loaded animation '{anim_data['name']}' "
                      f"({anim_data['duration']:.3f}s, {len(converted_tracks)} tracks)")

    return HavokPack(skeleton=skeleton, animations=animations, meshes=meshes)


# --- PAK / archive helpers (stubs) ---

def _extract_from_archive(path, entry, pak_profile, pak_platform):
    raise NotImplementedError("PAK archive support requires additional parsers")

def enumerate_pak_entries(path, profile_name, endianness):
    return []


# --- XML parsing helpers ---

def _read_text(parent, name, fallback=""):
    param = parent.find(f"hkparam[@name='{name}']")
    if param is not None and param.text:
        return param.text.strip()
    return fallback


def _read_vector(transform_param, name):
    if transform_param is None:
        return mathutils.Vector((0.0, 0.0, 0.0))
    param = transform_param.find(f"hkparam[@name='{name}']")
    if param is None or param.text is None:
        return mathutils.Vector((0.0, 0.0, 0.0))
    values = [float(v) for v in param.text.split()]
    return mathutils.Vector(values[:3])


def _read_quaternion(transform_param, name):
    if transform_param is None:
        return mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))
    param = transform_param.find(f"hkparam[@name='{name}']")
    if param is None or param.text is None:
        return mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))
    values = [float(v) for v in param.text.split()]
    if len(values) >= 4:
        return mathutils.Quaternion((values[3], values[0], values[1], values[2]))
    return mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))


def _parse_skeleton(root, override_name=None):
    skel_obj = root.find(".//hkobject[@class='hkaSkeleton']")
    if skel_obj is None:
        return None
    name_param = skel_obj.find("hkparam[@name='name']")
    if name_param is not None and name_param.text and name_param.text.strip():
        skel_name = name_param.text.strip()
    else:
        skel_name = override_name or "Skeleton"
    bones_param = skel_obj.find("hkparam[@name='bones']")
    bones = []
    if bones_param is not None:
        for idx, b in enumerate(bones_param.iterfind("hkobject")):
            bname = _read_text(b, "name", fallback=f"Bone_{idx}")
            parent = int(_read_text(b, "parent", fallback="-1"))
            transform = b.find("hkparam[@name='transform']")
            translation = _read_vector(transform, "translation")
            rotation = _read_quaternion(transform, "rotation").conjugated()
            scale = _read_vector(transform, "scale")
            if scale.length_squared < 0.0001:
                scale = mathutils.Vector((1.0, 1.0, 1.0))
            bones.append(HavokBone(name=bname, parent=parent,
                                   translation=translation, rotation=rotation, scale=scale))
    return HavokSkeleton(name=skel_name, bones=bones)


def _parse_meshes(root, override_name=None):
    meshes = []
    for obj in root.findall(".//hkobject"):
        verts_param = None
        for pname in ("vertices", "positions"):
            verts_param = obj.find(f"hkparam[@name='{pname}']")
            if verts_param is not None:
                break
        if verts_param is None:
            continue
        tris_param = None
        for pname in ("triangles", "indices", "indices16", "indices32"):
            tris_param = obj.find(f"hkparam[@name='{pname}']")
            if tris_param is not None:
                break
        if tris_param is None:
            continue
        vertices = []
        faces = []
        if verts_param.text:
            values = [float(v) for v in verts_param.text.split()]
            for i in range(0, len(values) - 2, 3):
                vertices.append(mathutils.Vector(values[i:i + 3]))
        if tris_param.text:
            tri_values = [int(v) for v in tris_param.text.split()]
            for i in range(0, len(tri_values) - 2, 3):
                faces.append((tri_values[i], tri_values[i + 1], tri_values[i + 2]))
        name = _read_text(obj, "name", fallback=f"Mesh_{len(meshes)}")
        meshes.append(HavokMesh(name=name, vertices=vertices, faces=faces))
    return meshes


def _parse_animations(root, skeleton=None):
    return []


def _decode_interleaved_tracks(transforms_param, num_tracks, num_frames):
    return []


def _parse_binding(binding, num_tracks, skeleton=None):
    return list(range(num_tracks))
