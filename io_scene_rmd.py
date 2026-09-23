"""Atlus RMD importer. Format research based on TGE's MaxScripts V4.1/V1.1.

See FORMAT.md for the byte layout, evidence, and unsupported variants.
The binary parser can also be imported without Blender for validation.
"""
bl_info = {
    "name": "Atlus RenderWare RMD", "author": "RMD importer contributors; format research by TGE",
    "version": (1, 0, 1), "blender": (4, 5, 0),
    "location": "File > Import > Atlus RenderWare (.rmd)",
    "description": "Import Atlus RenderWare models, skins, textures and animations",
    "category": "Import-Export",
}

import io
import logging
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field

try:
    import bpy
    from bpy.props import BoolProperty, FloatProperty, StringProperty
    from bpy_extras.io_utils import ImportHelper
    from mathutils import Matrix, Quaternion, Vector
except ModuleNotFoundError:
    bpy = None

LOG = logging.getLogger("io_scene_rmd")
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())
LOG.setLevel(logging.INFO)


class RmdError(ValueError):
    pass


class Reader:
    """Little-endian, bounded memory stream; every child has its own boundary."""
    def __init__(self, data, base=0):
        self.stream = io.BytesIO(data)
        self.size = len(data)
        self.base = base

    @property
    def remaining(self):
        return self.size - self.stream.tell()

    def read(self, count):
        if count < 0 or count > self.remaining:
            raise RmdError(f"Read of {count} bytes crosses boundary at 0x{self.base + self.stream.tell():X}")
        return self.stream.read(count)

    def unpack(self, fmt):
        values = struct.unpack("<" + fmt, self.read(struct.calcsize("<" + fmt)))
        if any(isinstance(v, float) and not math.isfinite(v) for v in values):
            raise RmdError("Non-finite floating point value in input")
        return values

    def u32(self):
        return self.unpack("I")[0]

    def array(self, fmt, count):
        size = struct.calcsize("<" + fmt)
        if count < 0 or count > self.remaining // size:
            raise RmdError(f"Invalid array count {count} at 0x{self.base + self.stream.tell():X}")
        return [self.unpack(fmt) for _ in range(count)]

    def chunks(self):
        while self.remaining:
            start = self.base + self.stream.tell()
            kind, size, version = self.unpack("III")
            yield Chunk(kind, version, self.read(size), start)


@dataclass
class Chunk:
    kind: int
    version: int
    data: bytes
    start: int = 0

    def reader(self):
        return Reader(self.data, self.start + 12)

    def children(self):
        return list(self.reader().chunks())

    def raw(self):
        return struct.pack("<III", self.kind, len(self.data), self.version) + self.data


def first(chunks, kind, required=False):
    result = next((c for c in chunks if c.kind == kind), None)
    if required and result is None:
        raise RmdError(f"Missing required chunk 0x{kind:X}")
    return result


def text_name(data):
    return data.split(b"\0", 1)[0].decode("cp932", errors="replace")


@dataclass
class Frame:
    matrix: tuple
    parent: int
    flags: int
    name_id: int = -1
    name: str = ""


@dataclass
class Geometry:
    vertices: list = field(default_factory=list)
    normals: list = field(default_factory=list)
    faces: list = field(default_factory=list)
    material_ids: list = field(default_factory=list)
    uvs: list = field(default_factory=list)
    colors: list = field(default_factory=list)
    materials: list = field(default_factory=list)
    indices: list = field(default_factory=list)
    weights: list = field(default_factory=list)
    inverse_bind: list = field(default_factory=list)


@dataclass
class Clump:
    frames: list = field(default_factory=list)
    hanim: dict = field(default_factory=dict)
    geometries: list = field(default_factory=list)
    atomics: list = field(default_factory=list)


@dataclass
class Animation:
    duration: float
    tracks: dict


@dataclass
class Document:
    clumps: list = field(default_factory=list)
    textures: dict = field(default_factory=dict)
    animations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def warn(self, message):
        self.warnings.append(message)
        LOG.warning(message)


def parse_frames(chunk, clump):
    children = chunk.children()
    r = first(children, 1, True).reader()
    count = r.u32()
    for values in r.array("12fii", count):
        clump.frames.append(Frame(values[:12], values[12], values[13]))
    extensions = [c for c in children if c.kind == 3]
    if len(extensions) != count:
        raise RmdError("Frame extension count does not match frame count")
    table = {}
    for i, ext in enumerate(extensions):
        clump.frames[i].name = f"Bone{i + 1}"
        h = first(ext.children(), 0x11E)
        if h is None:
            continue
        r = h.reader()
        version, name_id, nodes = r.unpack("Iii")
        clump.frames[i].name_id = name_id
        clump.frames[i].name = f"_{name_id}"
        if nodes:
            r.unpack("II")  # hierarchy flags, key frame size
            for node_id, index, flags in r.array("iii", nodes):
                if index < 0 or index in table:
                    raise RmdError("Invalid or duplicate HAnim index")
                table[index] = node_id
    ids = {f.name_id: i for i, f in enumerate(clump.frames) if f.name_id != -1}
    for index, name_id in table.items():
        if name_id not in ids:
            raise RmdError(f"HAnim node {name_id} has no frame")
        clump.hanim[index] = ids[name_id]
    # Validate hierarchy iteratively, including parents that occur later in file.
    for i in range(count):
        seen = set()
        j = i
        while j != -1:
            if j < 0 or j >= count or j in seen:
                raise RmdError(f"Invalid/cyclic frame parent chain from {i}")
            seen.add(j)
            j = clump.frames[j].parent


def parse_materials(chunk):
    children = chunk.children()
    materials = []
    for mat in (c for c in children if c.kind == 7):
        tex = first(mat.children(), 6)
        name = ""
        if tex:
            string = first(tex.children(), 2)
            if string:
                name = text_name(string.data)
        materials.append(name)
    # RW material list permits references to earlier slots; scripts ignore this.
    info = first(children, 1)
    if info:
        r = info.reader()
        count = r.u32()
        refs = [v[0] for v in r.array("i", count)]
        result, new = [], iter(materials)
        for ref in refs:
            if ref == -1:
                try:
                    result.append(next(new))
                except StopIteration:
                    raise RmdError("Material list has too few material chunks") from None
            elif 0 <= ref < len(result):
                result.append(result[ref])
            else:
                raise RmdError("Invalid material slot reference")
        return result
    return materials


def strip_triangles(indices):
    for i in range(2, len(indices)):
        a, b, c = indices[i - 2:i + 1]
        if i % 2:
            a, b = b, a
        if len({a, b, c}) == 3:
            yield (c, b, a)


def parse_geometry(chunk, doc):
    children = chunk.children()
    r = first(children, 1, True).reader()
    flags, uv_byte, native, faces, vertices, morphs = r.unpack("HBBIII")
    if native:
        raise RmdError("Native/platform geometry is not decoded by the supplied MaxScript")
    uv_count = uv_byte or (2 if flags & 0x80 else 1 if flags & 4 else 0)
    g = Geometry()
    if flags & 8:
        g.colors = r.array("4B", vertices)
    for _ in range(uv_count):
        g.uvs.append(r.array("2f", vertices))
    for a, b, mat, c in r.array("4H", faces):
        g.faces.append((c, b, a))
        g.material_ids.append(mat)
    if morphs == 0:
        raise RmdError("Geometry has no morph target")
    for target in range(morphs):
        r.unpack("4f")  # sphere, not the six-float bounding box guessed by MaxScript
        has_positions, has_normals = r.unpack("II")
        positions = r.array("3f", vertices) if has_positions else []
        normals = r.array("3f", vertices) if has_normals else []
        if target == 0:
            g.vertices, g.normals = positions, normals
    if len(g.vertices) != vertices:
        raise RmdError("First morph target has no vertex positions")
    if morphs > 1:
        doc.warn("Additional morph targets parsed but not imported as shape keys")
    mats = first(children, 8)
    if mats:
        g.materials = parse_materials(mats)
    ext = first(children, 3)
    plugins = ext.children() if ext else []
    skin = first(plugins, 0x116)
    if skin:
        r = skin.reader()
        bones, used, max_weights, pad = r.unpack("4B")
        r.read(used)
        g.indices = r.array("4B", vertices)
        g.weights = r.array("4f", vertices)
        g.inverse_bind = r.array("16f", bones)
        for ids, weights in zip(g.indices, g.weights):
            if any(w < 0 or w > 1 or (w > 0 and idx >= bones) for idx, w in zip(ids, weights)):
                raise RmdError("Invalid skin bone index or weight")
        # Split-skin/platform trailer is deliberately skipped within this chunk.
    binmesh = first(plugins, 0x50E)
    if not g.faces and binmesh:
        r = binmesh.reader()
        mode, count, total = r.unpack("III")
        if mode not in (0, 1):
            raise RmdError(f"Unsupported BinMesh primitive mode {mode}")
        actual = 0
        for _ in range(count):
            n, material = r.unpack("II")
            ids = [v[0] for v in r.array("I", n)]
            actual += n
            if mode == 0 and n % 3:
                raise RmdError("Triangle list index count is not divisible by three")
            triangles = list(strip_triangles(ids)) if mode else [tuple(reversed(ids[i:i+3])) for i in range(0, n, 3)]
            g.faces.extend(triangles)
            g.material_ids.extend([material] * len(triangles))
        if actual != total:
            raise RmdError("BinMesh total index count mismatch")
    if any(v >= vertices for tri in g.faces for v in tri):
        raise RmdError("Face index exceeds vertex count")
    if g.materials and any(m >= len(g.materials) for m in g.material_ids):
        raise RmdError("Face material index exceeds material slot count")
    return g


def decompress(value):
    # Atlus/RW custom 1/4/11 float; NOT IEEE binary16.
    bits = (value & 0x8000) << 16
    if value & 0x7FFF:
        bits |= ((value & 0x7800) << 12) + 0x38000000
        bits |= (value & 0x07FF) << 12
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def parse_animation(chunk):
    r = chunk.reader()
    version, kind, count, flags, duration = r.unpack("IIIIf")
    if kind not in (1, 2):
        raise RmdError(f"Unsupported animation keyframe type {kind}")
    if duration < 0:
        raise RmdError("Negative animation duration")
    stride = 36 if kind == 1 else 24
    physical = 36 if kind == 1 else 22
    if count > r.remaining // physical:
        raise RmdError("Animation key count exceeds chunk size")
    tracks, owners = {}, {}
    for i in range(count):
        time = r.unpack("f")[0]
        if kind == 1:
            quat = r.unpack("4f")
            pos = r.unpack("3f")
        else:
            quat = tuple(decompress(v) for v in r.unpack("4H"))
            pos = tuple(decompress(v) for v in r.unpack("3H"))
        previous = r.u32()
        if time < 0 or time > duration + 0.001:
            raise RmdError("Animation key time outside duration")
        if time == 0:
            owner = len(tracks)
            tracks[owner] = []
        else:
            if previous not in owners:
                raise RmdError(f"Invalid animation previous-frame offset 0x{previous:X}")
            owner = owners[previous]
        owners[i * stride] = owner
        tracks[owner].append((time, quat, pos))
    if kind == 2:
        offset, scalar = r.unpack("3f"), r.unpack("3f")
        for owner, keys in tracks.items():
            tracks[owner] = [(t, q, tuple(p[j]*scalar[j]+offset[j] for j in range(3))) for t, q, p in keys]
    return Animation(duration, tracks)


def parse(data, suffix=".rmd"):
    doc = Document()
    offset = 0x100 if suffix.lower() in (".pac", ".fpc", ".p01") else 0
    r = Reader(data[offset:], offset)
    if r.remaining >= 4 and data[offset:offset+4] == struct.pack("<I", 0xF0F000F0):
        r.read(14)  # 12-byte wrapper + u16 animation count
    while r.remaining:
        start = r.base + r.stream.tell()
        kind, size, version = r.unpack("III")
        chunk = Chunk(kind, version, r.read(size), start)
        if kind == 0xF0F000E0:
            r.read(2)  # observed particle wrapper special case in model script
        elif kind == 0x16:
            for tex in chunk.children():
                if tex.kind == 0x15:
                    name = first(tex.children(), 2)
                    if name:
                        doc.textures[text_name(name.data)] = tex.raw()
        elif kind == 0x10:
            clump = Clump()
            children = chunk.children()
            frames = first(children, 0xE)
            if frames:
                parse_frames(frames, clump)
            geoms = first(children, 0x1A)
            if geoms:
                clump.geometries = [parse_geometry(c, doc) for c in geoms.children() if c.kind == 0xF]
            for atomic in (c for c in children if c.kind == 0x14):
                rr = first(atomic.children(), 1, True).reader()
                frame, geometry, flags, unused = rr.unpack("iiII")
                if not 0 <= frame < len(clump.frames) or not 0 <= geometry < len(clump.geometries):
                    raise RmdError("Atomic references invalid frame or geometry")
                clump.atomics.append((frame, geometry))
            doc.clumps.append(clump)
        elif kind == 0x1B:
            doc.animations.append(parse_animation(chunk))
        else:
            LOG.debug("Skipping chunk 0x%X at 0x%X (%d bytes)", kind, start, size)
    if not (doc.clumps or doc.animations or doc.textures):
        raise RmdError("No supported RMD model, texture or animation chunks found")
    return doc


def frame_matrix(values, scale):
    return Matrix(((values[0], values[3], values[6], values[9]*scale),
                   (values[1], values[4], values[7], values[10]*scale),
                   (values[2], values[5], values[8], values[11]*scale), (0, 0, 0, 1)))


def world_matrices(clump, scale):
    result = {}
    for i in range(len(clump.frames)):
        chain, j = [], i
        while j != -1 and j not in result:
            chain.append(j)
            j = clump.frames[j].parent
        for j in reversed(chain):
            f = clump.frames[j]
            local = frame_matrix(f.matrix, scale)
            result[j] = result[f.parent] @ local if f.parent != -1 else local
    return result


def build_armature(context, clump, collection, name, scale, fix):
    matrices = world_matrices(clump, scale)
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    context.view_layer.objects.active = obj
    obj.select_set(True)
    obj.show_in_front = True
    bpy.ops.object.mode_set(mode="EDIT")
    names = {}
    try:
        for i, f in enumerate(clump.frames):
            bone = data.edit_bones.new(f.name)
            bone.head = matrices[i].translation
            bone.tail = bone.head + Vector((0, max(0.01*scale, 0.0001), 0))
            bone.matrix = matrices[i]
            if fix:
                distances = [(matrices[j].translation - bone.head).length for j, child in enumerate(clump.frames) if child.parent == i]
                distances = [v for v in distances if v > 1e-6]
                if distances:
                    bone.length = min(distances)
            names[i] = bone.name
        for i, f in enumerate(clump.frames):
            if f.parent != -1:
                data.edit_bones[names[i]].parent = data.edit_bones[names[f.parent]]
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    for i, f in enumerate(clump.frames):
        bone = data.bones[names[i]]
        bone["rmd_frame_index"] = i
        bone["rmd_name_id"] = f.name_id
        bone["rmd_source_local"] = list(f.matrix)
    for index, i in clump.hanim.items():
        data.bones[names[i]]["rmd_hanim_index"] = index
    obj["rmd_scale"] = scale
    return obj, names, matrices


class TextureLoader:
    def __init__(self, source, exporter, doc):
        self.source, self.exporter, self.doc = Path(source), exporter, doc
        self.temp = tempfile.TemporaryDirectory(prefix="rmd_")
        self.cache = {}

    def close(self):
        self.temp.cleanup()

    def load(self, name):
        if not name:
            return None
        if name in self.cache:
            return self.cache[name]
        self.cache[name] = None
        # Names from files cannot escape the temporary/external texture directory.
        safe = name.replace("\\", "/").rsplit("/", 1)[-1]
        safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in safe).strip(". ") or "texture"
        roots = [self.source.parent / (self.source.stem + "_tex"), self.source.parent]
        png = next((p / (safe + ".png") for p in roots if (p / (safe + ".png")).is_file()), None)
        if png is None:
            external = next((p / (safe + ".txn") for p in roots if (p / (safe + ".txn")).is_file()), None)
            raw = self.doc.textures.get(name)
            if raw is None and external is None:
                self.doc.warn(f"Texture not found: {name}")
                return None
            if not self.exporter or not Path(self.exporter).is_file():
                self.doc.warn(f"Set the RMDTextureExporter.exe path to convert texture: {name}")
                return None
            folder = Path(self.temp.name) / str(len(self.cache))
            folder.mkdir()
            txn = folder / (safe + ".txn")
            if raw is not None:
                txn.write_bytes(raw)
            else:
                shutil.copyfile(external, txn)
            try:
                result = subprocess.run([str(Path(self.exporter).resolve()), str(txn)], cwd=folder,
                                        capture_output=True, text=True, errors="replace", timeout=120,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
                if result.returncode:
                    raise RmdError(f"exit {result.returncode}: {result.stderr[-1000:]} {result.stdout[-1000:]}")
                candidates = list(folder.rglob("*.png"))
                png = txn.with_suffix(".png")
                if not png.is_file():
                    if len(candidates) != 1:
                        raise RmdError("Exporter produced no unambiguous PNG output")
                    png = candidates[0]
            except (OSError, subprocess.TimeoutExpired, RmdError) as exc:
                self.doc.warn(f"Texture conversion failed for {name}: {exc}")
                return None
        image = bpy.data.images.load(str(png), check_existing=False)
        image.pack()  # survive removal of temporary files and saving/reopening .blend
        self.cache[name] = image
        return image


def make_material(name, textures, colors):
    mat = bpy.data.materials.new(name or "RMD Material")
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    shader = nodes.get("Principled BSDF")
    shader.inputs["Roughness"].default_value = 0.7
    color, alpha = None, None
    if textures:
        image = textures.load(name)
        if image:
            tex = nodes.new("ShaderNodeTexImage")
            tex.image = image
            tex.location = (-600, 200)
            color, alpha = tex.outputs["Color"], tex.outputs["Alpha"]
    if colors:
        vc = nodes.new("ShaderNodeVertexColor")
        vc.layer_name = "Color"
        vc.location = (-600, -100)
        if color:
            mix = nodes.new("ShaderNodeMixRGB")
            mix.blend_type = "MULTIPLY"
            mix.inputs[0].default_value = 1
            links.new(color, mix.inputs[1])
            links.new(vc.outputs["Color"], mix.inputs[2])
            color = mix.outputs[0]
            mul = nodes.new("ShaderNodeMath")
            mul.operation = "MULTIPLY"
            links.new(alpha, mul.inputs[0])
            links.new(vc.outputs["Alpha"], mul.inputs[1])
            alpha = mul.outputs[0]
        else:
            color, alpha = vc.outputs["Color"], vc.outputs["Alpha"]
    if color:
        links.new(color, shader.inputs["Base Color"])
    if alpha:
        links.new(alpha, shader.inputs["Alpha"])
        mat.surface_render_method = "DITHERED"
    return mat


def build_mesh(clump, geometry, frame, matrices, arm, names, collection, name, options, textures):
    g = geometry
    transform = matrices[frame] if frame is not None else Matrix.Identity(4)
    scale = options.scale_factor
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([transform @ (Vector(v)*scale) for v in g.vertices], [], g.faces)
    mesh.polygons.foreach_set("use_smooth", [True]*len(g.faces))
    slots = g.materials or [""] * (max(g.material_ids, default=0) + 1)
    for material in slots:
        mesh.materials.append(make_material(material, textures, options.import_vertex_colors and bool(g.colors)))
    mesh.polygons.foreach_set("material_index", g.material_ids)
    for i, uvs in enumerate(g.uvs):
        layer = mesh.uv_layers.new(name=f"UVMap{i + 1}")
        layer.data.foreach_set("uv", [c for loop in mesh.loops for c in (uvs[loop.vertex_index][0], 1.0 - uvs[loop.vertex_index][1])
    if options.import_vertex_colors and g.colors:
        layer = mesh.color_attributes.new(name="Color", type="BYTE_COLOR", domain="CORNER")
        layer.data.foreach_set("color_srgb", [c/255 for loop in mesh.loops for c in g.colors[loop.vertex_index]])
    mesh.update()
    if g.normals:
        normal_matrix = transform.to_3x3().inverted_safe().transposed()
        mesh.normals_split_custom_set_from_vertices([(normal_matrix @ Vector(n)).normalized() for n in g.normals])
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    if arm:
        obj.parent = arm
        modifier = obj.modifiers.new("RMD Skin", "ARMATURE")
        modifier.object = arm
        if g.weights:
            mapping = clump.hanim or {i: i for i in range(len(clump.frames))}
            groups = {}
            for vertex, (indices, weights) in enumerate(zip(g.indices, g.weights)):
                combined = {}
                for index, weight in zip(indices, weights):
                    if weight:
                        if index not in mapping:
                            raise RmdError(f"Skin index {index} missing from HAnim hierarchy")
                        bone = names[mapping[index]]
                        combined[bone] = combined.get(bone, 0.0) + weight
                for bone, weight in combined.items():
                    if bone not in groups:
                        groups[bone] = obj.vertex_groups.new(name=bone)
                    groups[bone].add([vertex], weight, "REPLACE")
        elif frame is not None:
            obj.vertex_groups.new(name=names[frame]).add(list(range(len(g.vertices))), 1.0, "REPLACE")
    return obj


def animation_nodes(arm):
    indexed = {int(b["rmd_hanim_index"]): b.name for b in arm.data.bones if "rmd_hanim_index" in b}
    if indexed:
        return indexed
    root = arm.data.bones.get("_5002") or arm.data.bones.get("Bone2")
    if root is None:
        raise RmdError("Animation target has no HAnim metadata or supported fallback root")
    result, stack = {}, [root]
    while stack:
        bone = stack.pop()
        result[len(result)] = bone.name
        stack.extend(reversed(list(bone.children)))
    return result


def build_actions(context, animations, arm, name, scale):
    mapping = animation_nodes(arm)
    fps = context.scene.render.fps / context.scene.render.fps_base
    arm.animation_data_create()
    original = arm.animation_data.action
    first_action = None
    for ai, animation in enumerate(animations):
        action = bpy.data.actions.new(f"{name}_{ai + 1:03}")
        action.use_fake_user = True
        slot = action.slots.new(id_type="OBJECT", name=arm.name)
        layer = action.layers.new("RMD Animation")
        bag = layer.strips.new(type="KEYFRAME").channelbag(slot, ensure=True)
        for index, keys in animation.tracks.items():
            if index not in mapping:
                raise RmdError(f"Animation track {index} has no matching bone")
            bone = arm.data.bones[mapping[index]]
            pb = arm.pose.bones[bone.name]
            pb.rotation_mode = "QUATERNION"
            rest_local = bone.parent.matrix_local.inverted() @ bone.matrix_local if bone.parent else bone.matrix_local
            samples, previous_q = [], None
            for time, xyzw, pos in sorted(keys):
                q = Quaternion((xyzw[3], xyzw[0], xyzw[1], xyzw[2]))
                if q.magnitude < 1e-8:
                    raise RmdError("Animation contains a zero quaternion")
                q.normalize()
                # RW XYZW becomes Blender WXYZ directly. MaxScript's inverse
                # compensates for Max's quaternion convention; copying that
                # inverse into Blender double-inverts the source rotation.
                local = Matrix.Translation(Vector(pos)*scale) @ q.to_matrix().to_4x4()
                loc, rot, size = (rest_local.inverted() @ local).decompose()
                if previous_q is not None and previous_q.dot(rot) < 0:
                    rot.negate()
                previous_q = rot.copy()
                samples.append((time*fps, loc, rot, size))
            for prop, sample_index, components in (("location", 1, 3), ("rotation_quaternion", 2, 4), ("scale", 3, 3)):
                for component in range(components):
                    curve = bag.fcurves.new(pb.path_from_id(prop), index=component)
                    curve.keyframe_points.add(len(samples))
                    curve.keyframe_points.foreach_set("co", [x for s in samples for x in (s[0], s[sample_index][component])])
                    for key in curve.keyframe_points:
                        key.interpolation = "LINEAR"
                    curve.update()
        action.use_frame_range = True
        action.frame_start, action.frame_end = 0, max(1, animation.duration*fps)
        if first_action is None:
            first_action = (action, slot)
    if first_action and original is None:
        arm.animation_data.action, arm.animation_data.action_slot = first_action
        context.scene.frame_start = 0
        context.scene.frame_end = math.ceil(first_action[0].frame_end)
        context.scene.frame_set(0)


def import_rmd(context, options):
    path = Path(options.filepath)
    wm = context.window_manager
    wm.progress_begin(0, 100)
    textures = None
    # Transactional cleanup removes only newly created IDs on failure.
    pools = ("objects", "collections", "meshes", "armatures", "materials", "images", "actions")
    before = {key: set(getattr(bpy.data, key)) for key in pools}
    old_active = context.view_layer.objects.active
    old_selected = list(context.selected_objects)
    scene_range = (context.scene.frame_start, context.scene.frame_end, context.scene.frame_current)
    target = old_active if old_active and old_active.type == "ARMATURE" else None
    target_modes = {b.name: b.rotation_mode for b in target.pose.bones} if target else {}
    try:
        if context.mode != "OBJECT":
            raise RmdError("Switch to Object Mode before importing")
        doc = parse(path.read_bytes(), path.suffix)
        wm.progress_update(15)
        collection = bpy.data.collections.new(path.stem)
        context.scene.collection.children.link(collection)
        exporter = options.texture_exporter
        if not exporter:
            entry = context.preferences.addons.get(__name__)
            exporter = entry.preferences.texture_exporter if entry else ""
        if not exporter:
            candidate = Path(__file__).parent / "RMD_Maxscript_V4/TextureExporter/RMDTextureExporter.exe"
            exporter = str(candidate) if candidate.is_file() else ""
        if options.import_textures:
            textures = TextureLoader(path, bpy.path.abspath(exporter) if exporter else "", doc)
        arms = []
        for ci, clump in enumerate(doc.clumps):
            name = f"{path.stem}_{ci + 1}"
            arm, names = None, {}
            matrices = world_matrices(clump, options.scale_factor)
            if options.import_skeleton and clump.frames:
                for obj in context.selected_objects:
                    obj.select_set(False)
                arm, names, matrices = build_armature(context, clump, collection, name + "_Rig", options.scale_factor, options.fix_bones)
                arms.append(arm)
            if options.import_mesh:
                draws = clump.atomics or [(None, i) for i in range(len(clump.geometries))]
                for di, (frame, gi) in enumerate(draws):
                    build_mesh(clump, clump.geometries[gi], frame, matrices, arm, names, collection,
                               f"{name}_Mesh{di + 1}", options, textures)
            wm.progress_update(15 + 65*(ci + 1)/max(1, len(doc.clumps)))
        if options.import_animations and doc.animations:
            if len(arms) > 1:
                doc.warn("Animation-to-clump association is not specified; import animations separately with one rig selected")
            elif arms or target:
                arm = arms[0] if arms else target
                build_actions(context, doc.animations, arm, path.stem, arm.get("rmd_scale", options.scale_factor))
            else:
                doc.warn("Animations found, but no imported or selected armature is available")
        if not collection.objects:
            bpy.data.collections.remove(collection)
        wm.progress_update(100)
        LOG.info("Imported %s: %d clumps, %d animations, %d warnings", path.name, len(doc.clumps), len(doc.animations), len(doc.warnings))
        return doc
    except Exception:
        if context.object and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for key in pools:
            pool = getattr(bpy.data, key)
            for item in list(pool):
                if item not in before[key]:
                    pool.remove(item, do_unlink=True)
        for obj in old_selected:
            obj.select_set(True)
        if target:
            for name, mode in target_modes.items():
                target.pose.bones[name].rotation_mode = mode
        context.view_layer.objects.active = old_active
        context.scene.frame_start, context.scene.frame_end = scene_range[:2]
        context.scene.frame_set(scene_range[2])
        raise
    finally:
        if textures:
            textures.close()
        wm.progress_end()


if bpy:
    class RMDPreferences(bpy.types.AddonPreferences):
        bl_idname = __name__
        texture_exporter: StringProperty(name="RMDTextureExporter.exe", subtype="FILE_PATH")

        def draw(self, context):
            self.layout.prop(self, "texture_exporter")

    class IMPORT_SCENE_OT_rmd(bpy.types.Operator, ImportHelper):
        bl_idname = "import_scene.rmd"
        bl_label = "Import Atlus RenderWare"
        bl_options = {"REGISTER", "UNDO"}
        filename_ext = ".rmd"
        filter_glob: StringProperty(default="*.rmd;*.rws;*.anm;*.pac;*.fpc;*.p01", options={"HIDDEN"})
        scale_factor: FloatProperty(name="Scale Factor", default=1.0, min=0.000001, max=100000.0)
        texture_exporter: StringProperty(name="RMDTextureExporter.exe", subtype="FILE_PATH")
        import_mesh: BoolProperty(name="Import Mesh", default=True)
        import_skeleton: BoolProperty(name="Import Skeleton", default=True)
        import_textures: BoolProperty(name="Import Textures", default=True)
        import_animations: BoolProperty(name="Import Animations", default=True)
        import_vertex_colors: BoolProperty(name="Import Vertex Colors", default=True)
        fix_bones: BoolProperty(name="Recalculate Bone Lengths", default=True,
                               description="Set lengths from child distances while preserving rest orientation")

        def execute(self, context):
            try:
                doc = import_rmd(context, self)
                if doc.warnings:
                    self.report({"WARNING"}, f"Imported with {len(doc.warnings)} warning(s): {doc.warnings[0]}")
                else:
                    self.report({"INFO"}, "RMD import completed")
                return {"FINISHED"}
            except Exception as exc:
                LOG.exception("RMD import failed")
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

    def menu_import(self, context):
        self.layout.operator(IMPORT_SCENE_OT_rmd.bl_idname, text="Atlus RenderWare (.rmd)")

    def register():
        bpy.utils.register_class(RMDPreferences)
        bpy.utils.register_class(IMPORT_SCENE_OT_rmd)
        bpy.types.TOPBAR_MT_file_import.append(menu_import)

    def unregister():
        bpy.types.TOPBAR_MT_file_import.remove(menu_import)
        bpy.utils.unregister_class(IMPORT_SCENE_OT_rmd)
        bpy.utils.unregister_class(RMDPreferences)

    if __name__ == "__main__":
        register()
