# Atlus RMD Importer for Blender

Import Atlus RenderWare `.rmd` models into **Blender 4.5 LTS**, including meshes,
armatures, skin weights, textures, and embedded animations. Based on TGE's
RenderWare model and animation MaxScripts.

## Features

- Meshes with material assignments, custom normals, and multiple UV layers.
- Armatures with HAnim bone mapping and vertex weights.
- Vertex colors when present in the source file.
- Embedded or external TXN texture conversion through `RMDTextureExporter.exe`.
- Node-based materials and packed image textures.
- Separate Blender Actions for supported animation sequences.
- Controls for scale and optional mesh, skeleton, texture, animation, and color import.

## Requirements

- **Blender 4.5 LTS**. Tested with Blender 4.5.10 on Windows.
- **Windows for the external texture exporter**, which is an external `.exe`.
  Automatic conversion on macOS and Linux is not configured by this add-on.
- `RMDTextureExporter.exe` and `AtlusLibSharp.dll` for TXN conversion. These are
  optional when importing geometry only or using preconverted PNG textures.

## Installation

1. Download `io_scene_rmd.py` from this repository.
2. In Blender, open **Edit > Preferences > Add-ons**.
3. Open the menu in the upper-right corner and choose **Install from Disk**.
4. Select `io_scene_rmd.py`, then enable **Atlus RenderWare RMD**.
5. Configure the texture exporter below if you need automatic texture conversion.

After installing an updated version, restart Blender and reimport your model.
Updating the add-on does not modify objects imported previously.

## Texture exporter setup

The add-on delegates TXN decoding to an external utility. The exporter and its DLL must be obtained separately; they are not included in this repository.

### Set the exporter path

1. Open **Edit > Preferences > Add-ons** and find **Atlus RenderWare RMD**.
2. Expand the add-on's settings.
3. Set **RMDTextureExporter.exe** to the location of the executable on your computer.
4. Save your preferences if automatic preference saving is disabled.

Keep `AtlusLibSharp.dll` in the same folder as the executable.

### Import with textures enabled

Choose **File > Import > Atlus RenderWare (.rmd)** and leave **Import Textures**
enabled. The add-on will:

1. Look for an existing PNG matching each referenced texture name.
2. If no PNG is found, extract an embedded TXN or copy a matching external TXN
   into a temporary directory.
3. Run the exporter and load the generated PNG.
4. Pack the image into Blender and remove the temporary files.

Save your `.blend` file to retain the packed images. You do not need to preserve
the temporary TXN/PNG files. Use **Material Preview** or **Rendered** viewport
shading to inspect the imported materials.

### Using preconverted textures

PNG and external TXN files are searched in `<model_name>_tex` first, then beside
the model. Filenames must match the texture names referenced inside the RMD,
which may differ from the model filename:

```text
models/
    character.rmd
    character_tex/
        body.png
        face.png
```

Keep **Import Textures** enabled when using existing PNGs. The exporter is not
required for textures found in PNG form.

## Importing models and animations

1. Switch to **Object Mode**.
2. Choose **File > Import > Atlus RenderWare (.rmd)**.
3. Select your model and adjust the import options.
4. Click **Import Atlus RenderWare**.

| Option | Behavior |
| --- | --- |
| Scale Factor | Scales model coordinates and animation translations; default is 1.0. |
| Import Mesh | Creates mesh objects. |
| Import Skeleton | Creates an armature and assigns mesh weights. |
| Import Textures | Loads existing PNGs or converts available TXNs. |
| Import Animations | Creates Actions for supported animation chunks. |
| Import Vertex Colors | Creates a color attribute only when the RMD contains color data. |
| Recalculate Bone Lengths | Sets bone display lengths from child distances while preserving rest orientation. |

Each embedded animation becomes a separate Action. Select a sequence in the
**Dope Sheet > Action Editor**. The first imported Action is assigned if the
armature has no active Action; existing active Actions are retained. Timing uses
the scene's FPS, so set it before importing.

To import a separate animation file, select an armature previously created by the
add-on and import the animation with **Import Animations** enabled. Files with
multiple clumps do not have their animation-to-rig association resolved
automatically; load animations separately with the intended rig selected.

## Compatibility and limitations

**This tool has not been tested for game modding and is likely unsuitable for that purpose.** It imports assets for use in Blender; it does not export RMD files or guarantee that edited models can be converted back into a game-compatible format. Do not assume that imported geometry, materials, rigging, or animations preserve everything required for reinsertion into a game.

Version 1.0.1 was validated against two Persona 3 FES samples, `FC_01_00.RMD` and
`FC_01_01.RMD`, including actual TXN conversion, packed textures, rigging, and
animations. Both imported four meshes, 124 bones, six textures, and nine Actions
without importer warnings. 

**This sample coverage does not guarantee compatibility with every Persona or Shin Megami Tensei PS2 title.**

The importer does not currently support native/platform-specific geometry,
particles, additional morph targets as shape keys, undocumented animation key
types, material effects, or undocumented Euler/scale animation records. Animation
interpolation may differ from the original game. Source axes are preserved.

See [FORMAT.md](FORMAT.md) for binary layouts, interpretation decisions, and
remaining format limitations.

## Credits

Original format research and reference MaxScripts by **TGE**:

- `RMD_ModelImporter_V4_1.ms`
- `RMD_AnimationImporter_V1_1.ms`

`RMDTextureExporter.exe` and `AtlusLibSharp.dll` are external dependencies and are
not included in this repository. Game models and textures belong to
their respective owners.



## Disclaimer

This add-on and the format analysis were created entirely by ChatGPT 6 Astra. They may contain errors, incorrect assumptions, or incomplete interpretations. Verify the code and format details independently before relying on them.
