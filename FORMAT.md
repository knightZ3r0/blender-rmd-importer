# Atlus RMD layout and porting notes

Primary evidence: TGE's `RMD_ModelImporter_V4_1.ms`,
`RMD_AnimationImporter_V1_1.ms`, and `include/MaxScriptExtensions/FileStreamReader.ms`
in the supplied `RMD_Maxscript_V4` directory. Credit for the original format
research belongs to TGE. This document distinguishes direct observations from
RenderWare interpretations and extensions beyond those scripts.

## Binary conventions

All fields are little endian. `readByte #unsigned` is u8, `readShort #unsigned`
is u16, `readLong` is i32 (or u32 for IDs/counts), and `readFloat` is IEEE f32.
Parent -1 must remain signed. Face indices are treated as unsigned even though
the source script omits `#unsigned`; Python indices remain zero based throughout.
Strings are bounded, NUL-terminated/padded byte strings, decoded as CP932 with
replacement for invalid sequences. No string from the file becomes an executable
command or an unrestricted output path.

Each ordinary chunk has a 12-byte header:

| Offset | Field | Type |
| --- | --- | --- |
| 0 | chunk ID | u32 |
| 4 | payload size, excludes header | u32 |
| 8 | RenderWare version/build value | u32 |
| 12 | payload | size bytes |

The MaxScript calls the final header word `DataSeperator`; it does not decode it.
Chunk end is start + 12 + size (model lines 39–71). The port creates bounded
child readers, so unknown extensions can be skipped without losing alignment.
Only known container chunks are recursively interpreted; arbitrary opaque data
is never scanned for coincidental chunk signatures.

Wrapper handling (model lines 207–234): raw RW starts at zero; FPC/P01 start at
0x100. PAC starts at 0x100, followed by an RMD wrapper where present. The wrapper
has u32 magic `0xF0F000F0`, eight ignored bytes, then u16 animation count, making
14 bytes. The sign-extended hex literal in MaxScript refers to this 32-bit magic.
The particle wrapper `0xF0F000E0` skips payload plus two bytes (line 837); particles
are not built. No general PAC archive directory is implemented.

## Model hierarchy

```text
0x16 Texture dictionary
  0x01 Dictionary metadata (ignored)
  0x15 Native texture
    0x02 Name (first string), mask name (second string)
    0x01 Native image/platform data (opaque to this importer)
0x10 Clump / scene
  0x01 Scene information (atomic count, other values)
  0x0E Frame list
    0x01 Frame array
    0x03 Extension, one per frame
      0x11E HAnim
  0x1A Geometry list
    0x01 Geometry count
    0x0F Geometry
      0x01 Geometry arrays
      0x08 Material list
        0x01 Material references
        0x07 Material
          0x01 Material properties (not interpreted by the source)
          0x06 Texture reference
            0x01 Sampler information
            0x02 Diffuse texture name, then mask name
      0x03 Extensions
        0x116 Skin
        0x50E BinMesh
        0x11D Other platform data (skipped)
  0x14 Atomic / draw node
    0x01 Frame and geometry association
0x1B Animation
```

### Frames and HAnim (model lines 337–423, 850–891)

Frame struct: u32 count; each frame is twelve f32 values (four rows of three),
i32 parent, and u32 flags: **56 bytes per frame**. The fourth row is translation.
The row-vector matrix is transposed into Blender's column-vector convention;
world = parent world @ local. Scale Factor multiplies translations and mesh
coordinates once, and animation translations by the same amount.

Each frame extension may contain HAnim: u32 version, i32 name ID, i32 node count.
If count is nonzero: u32 hierarchy flags, u32 keyframe size, then count records
of (i32 name ID, i32 HAnim index, u32 flags). A second pass connects HAnim indices
to frames by name ID. Bone labels are `_ID`, with `BoneN` for unnamed frames.
The parser validates missing mappings, parent ranges, and parent cycles.

Bone length correction changes display length only, preserving the rest basis.
Blender bones use local Y for their visible direction; source matrices remain
the authoritative rest orientation. Arbitrary shear/nonuniform scale in frame
matrices is not faithfully representable as Blender edit-bone rest matrices.

### Geometry (model lines 438–545)

| Field | Encoding |
| --- | --- |
| Format flags | u16 |
| UV channel count | u8 |
| Native flag | u8 |
| Triangle count | u32 |
| Vertex count | u32 |
| Morph-target count | u32 |
| Prelit colors, if flags & 8 | vertex count × RGBA u8 |
| UV channels | channel count × vertex count × (f32 u, f32 v) |
| Triangles | triangle count × (u16 a, u16 b, u16 material, u16 c) |
| Morph target header | four f32 bounding sphere values, u32 positions present, u32 normals present |
| Positions, if present | vertex count × three f32 |
| Normals, if present | vertex count × three f32 |

The source reads the UV/native pair as one u16 and the morph count as `Unknown1`.
It calls the next 24 bytes a six-float bounding box, then unconditionally reads
positions/normals. The port uses the standard RW interpretation of the same
24 bytes (sphere and two presence flags); this is an interpretation beyond the
source's labels and needs confirmation against actual game assets. The common
one-target, positions-and-normals case has identical offsets. Only the first
morph target is built; additional targets produce a warning.

Triangles become `(c,b,a)`, matching the script, and UV V becomes `-v`.
Every UV layer is retained; the original scene-building loop only uses layer 1.
All faces are smooth (source smoothing group 1); there is no smoothing-group
array in this layout. Custom normals are transformed by inverse transpose.
All RGBA components are preserved, including alpha discarded by the source.

The script skips 0x50E. The port additionally supports the conventional nonnative
RW BinMesh layout: u32 primitive mode (0=list, 1=strip), u32 mesh count, u32 total
indices; each mesh has u32 index count, u32 material, then u32 indices. It is used
only when the ordinary triangle array is empty. Strip winding alternates and
degenerate triangles are discarded. This extension has synthetic coverage only.
Native console/platform-specific geometry is explicitly rejected.

### Materials and textures (model lines 242–317, 546–610, 923–943)

Material list struct is interpreted as u32 slot count followed by i32 references:
-1 introduces a material, a nonnegative index reuses an earlier slot. The original
script ignores this mapping and collects texture names; the port retains untextured
slots as well. The first 0x02 child of a texture reference names the diffuse map.
Other material properties and effects are not decoded.

Texture export writes **the entire 0x15 chunk, including its 12-byte header**, to
a temporary `.txn`. It does not write just the image struct or texture dictionary.
The supplied executable is invoked with one absolute TXN argument, no shell,
a hidden process window, captured diagnostics, and a 120-second timeout. External
PNGs/TXNs are searched next to the source and in `<source_stem>_tex`. External TXNs
are copied to the temporary directory before conversion. All loaded images are
packed before temporary data is removed. The exporter and AtlusLibSharp.dll must
remain together. Conversion requires an environment capable of running that EXE.

### Skin and atomics (model lines 687–785, 804–829, 945–1004)

Skin payload: u8 total bones, u8 used-bone count, two additional bytes (the source
reads these as i16 `flag`), used-bone count bytes, vertex count × four u8 bone
indices, vertex count × four f32 weights, total bones × sixteen f32 inverse-bind
matrix values. Remaining split-skin/platform data is skipped to the chunk end.
The source interprets flag 3 trailers, but does not use them for the resulting rig.

Weight indices address HAnim order, not frame-list order. Positive weights are
copied without normalization; duplicate influences are summed. Matrices are
retained by the parser but, following the MaxScript, rest frames plus atomic
transform determine Blender's bind pose. Models requiring a distinct inverse-bind
pose remain an unverified variant. Missing HAnim uses frame order as a fallback.

Atomic struct: i32 frame index, i32 geometry index, u32 flags, u32 unused. Each
atomic creates a mesh instance with its frame world transform baked into geometry.
Unskinned instances receive rigid weight 1 on the atomic frame if a rig is built.
Clumps keep separate frame/geometry/HAnim namespaces, avoiding the original global
array offset and bone-name collision problems.

## Animations

Source animation lines 100–213 specify the stream; 217–251 apply keys; 318–329
decompress values; 334–369 resolve animation node order.

Animation payload header: u32 version, u32 key type, u32 total key count,
u32 flags, f32 duration in seconds (**20 bytes**).

| Type | Key record | Physical bytes | Previous-key address stride |
| --- | --- | --- | --- |
| 1 | f32 time, four f32 quaternion XYZW, three f32 translation, u32 previous offset | 36 | 36 |
| 2 | f32 time, four u16 quaternion, three u16 translation, u32 previous offset | 22 | 24 |

Type 2 ends with three f32 translation offsets and three f32 translation scalars.
Decoded translation = compressed translation × scalar + offset, componentwise.
The 22/24 distinction follows the script's exact reads and offset table; do not
insert two padding bytes into on-disk keys. Type 2 uses a custom sign/exponent/
mantissa encoding, not IEEE half precision:

```python
bits = (value & 0x8000) << 16
if value & 0x7fff:
    bits |= ((value & 0x7800) << 12) + 0x38000000
    bits |= (value & 0x07ff) << 12
# reinterpret bits as IEEE float32
```

Initial time-zero keys establish tracks in HAnim order. Later keys inherit their
track through the previous offset. Offsets refer to the key array, not the file.
Quaternion order is converted from XYZW to Blender WXYZ directly. The MaxScript's
explicit inverse compensates for Max's quaternion convention and must not be
copied into Blender. Real Persona samples confirmed this: the old inverse twisted
the character, while direct conversion reproduces static rotated bone bases and
coherent animated poses. The local animated transform is converted into
Blender pose basis with inverse(local rest) @ animated local. Quaternion signs
are made continuous before writing linear curves. Times use scene FPS/FPS base.

There are **no Euler or animated-scale records in either supplied script**.
The add-on does not invent such records: it writes decomposed pose-basis scale
channels but cannot import an unknown scale-key format. Quaternion component
curves use linear interpolation; this is not an exact reproduction of Max TCB
rotation interpolation between keys. Each sequence becomes a separate slotted
Action with a fake user. The first is assigned if the rig has no active Action.
For multiple clumps, automatic animation association is deliberately skipped with
a warning because no association is specified by the reference.

The slotted Action implementation follows Blender's
[Action API migration documentation](https://developer.blender.org/docs/release_notes/4.4/upgrading/slotted_actions/).
Material transparency uses `surface_render_method`, as documented in the
[Blender Python API changes](https://developer.blender.org/docs/release_notes/4.2/python_api/).

## Validation boundaries

Development tests (not included in this repository) generated independent small binary fixtures and ran actual
Blender imports. They verify array layout, bounds rejection, HAnim mapping,
compressed link offsets, wrapper handling, strips, geometry placement, actual
animated deformation, packed PNG material creation, import toggles, rollback,
and operator registration. All 11 synthetic tests pass in Blender 4.5.10 LTS.

Subsequently supplied `FC_01_00.RMD` and `FC_01_01.RMD` both passed real-file
integration tests, including actual embedded TXN conversion with the external
exporter and packed-image reload when opening the saved blends. Each contains
four meshes, 124 bones, six textures, and nine animations. Mesh totals are
1,743 vertices / 1,745 triangles and 1,672 vertices / 1,631 triangles respectively.
Every Action was evaluated at start, midpoint, and end for finite geometry;
rest-pose and animation renders were generated for visual inspection. A static
rotated source bone is compared against its evaluated animated local basis to
regress the quaternion-convention bug fixed in 1.0.1. This does not establish
compatibility with every Persona or SMT game or exact in-game interpolation.


## Disclaimer

This add-on and the format analysis were created entirely by ChatGPT 6 Astra. They may contain errors, incorrect assumptions, or incomplete interpretations. Verify the code and format details independently before relying on them.
