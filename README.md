# BlenderToolsForGFS
A Blender 2.81+ plugin for importing and exporting GFS and GAP files.

A collection of preset materials, which you should use alongside this plugin to maximise your likelihood of a successful export, can be found in MadMax1960's [GFD Asset Library](https://github.com/MadMax1960/gfd-asset-library) repository.

<!--
#### ⚠ IMPORTANT NOTE ⚠

The export of models using this plugin is idiomatic and require a very specific arrangement of data and objects. Please [READ THE DOCUMENTATION]() (link incomplete for now, will link to documentation when written) to learn how to export models using the plugin.

You can also access the documentation from within Blender by inspecting the drop-down menu for the plugin in the Blender Preferences/Addons menu and clicking the link to the documentation, or by opening the PDF in the `docs` folder of the plugin repository.

#### ⚠ IMPORTANT NOTE ⚠
-->

## README Table of Contents
| Section | Contents |
|---|---|
| [Plugin Installation](#plugin-installation) | Important notes on plugin installation beyond regular addon registry. |
| [Unity FBX preview export](#unity-fbx-preview-export) | Export a Unity-ready FBX with PNG textures from the GFS Tools sidebar. |
| [Known Unity FBX bugs](#known-unity-fbx-bugs) | What vanilla / Goo / old exporters get wrong, and what this fork fixes. |
| [Plugin Usage](#plugin-usage) | Important notes on using the plugin. |
| [Limitations](#limitations) | Plugin limitations. |
| [Future Development](#future-development) | Notes on the most important features that are missing from the plugin. |
| [Acknowledgements](#acknowledgements) | Important acknowledgements of assistance and resources used in the development of this library. |
| [Supported Formats](#supported-formats) | Formats supported by the plugin. |

## Plugin Installation
The plugin comes bundled with documentation. In the source repository, this is just a LaTeX file plus the required images to build it, and the buttons in the Blender UI designed to open it will fail if you just install the addon from the develop branch. Therefore you have two options for install:
- Install the latest release.
- Download the code, and either:
    - Build the LaTeX file from source (out of scope for this README).
    - Unzip the addon, take the `Documentation.pdf` from the latest release, put in the `docs` folder of the downloaded code, zip the addon back up and install it.

## Unity FBX preview export

The GFS Tools sidebar has two Unity preview buttons. This is **not** a GMD/GAP round-trip. Game files still use File > Export > GFS.

### Export FBX with PNG Textures

Single file. Use this when **one** GAP is already on the character (or you really do want every loaded Action in one FBX).

- Writes PNG maps into a sibling `{name}.fbm` folder (Unity Import Standard looks there). Do **not** use `_textures`.
- Bakes Actions / NLA strips.
- `FBX_SCALE_ALL` so Unity does not treat the file as centimetres.
- Blender native `Y` forward / `Z` up. Unity `Bake Axis Conversion` does the rest.
- Hides `Blur*` outline meshes and temporarily renames meshes that collide with bone names.
- If the destination is under a Unity `Assets/` folder, writes/patches `.fbx.meta`: Import Standard, Local materials, import animation, Bake Axis Conversion.

### Batch Export GAP FBX

**One FBX per GAP**, named like the existing Unity library:

- `芳泽霞_BF251.fbx` ← `BF0010_251.GAP`
- `奥村春_BF251.fbx` / `奥村春_BF253.fbx` ← `BF0010_251.GAP` / `BF0010_253.GAP`
- `奥村春_AF400.fbx` ← `AF0010_400.GAP`

Typical flow:

1. Import the character GMD (e.g. 芳泽霞 / `C0010_004_00`).
2. Set **Character Name** to `芳泽霞` (or `奥村春`).
3. Set **GAP Folder** to that character's FIELD dir, e.g. `...\MODEL\CHARACTER\0010\FIELD`. Leave empty to export packs already imported on the armature.
4. Click **Batch Export GAP FBX** and pick the Unity output folder (e.g. `Assets/Anim/Persona5/Haru Okumura`).

Each GAP is isolated before export: only that pack's **BASE** clips (+ Rest Pose) go into that FBX. LOOKAT / BLEND are off unless you tick them in the file dialog. Do **not** dump every FIELD anim into one file.

### Known Unity FBX bugs

These are the failures we hit with vanilla Blender / Goo Engine `File > Export > FBX` and with the original plugin (no Unity path). The sidebar exporters above exist because of them.

| Bug | What you see in Unity | Cause | This fork |
|---|---|---|---|
| Scale 100× / exploding skin | Bones `lossyScale = 100`, shoes or feet blow up when skinned | Goo `FBX_SCALE_NONE` writes `UnitScaleFactor = 0.01`. Unity treats that as centimetres and compensates on the skeleton | `apply_scale_options='FBX_SCALE_ALL'` so file scale is 1 |
| One heel stretched | Left/right `Bip01 Foot` bindposes stop mirroring; heel weights pull one shoe | Goo `-Z` forward / `Y` up rewrites Foot bindposes incorrectly | Export Blender `Y`/`Z` axes; Unity bakes the conversion |
| Pink / white materials | 0 textures on every slot | PNG dumped to `{name}_textures`, or Unity **On Demand Remap** which ignores PNG | Copy into `{name}.fbm` + `path_mode='COPY'`; `.meta` **Import Standard** + **Local** |
| Diffuse not on Standard | Lit grey even when PNG exist | GFS materials leave Principled **Base Color** unconnected | Connect Diffuse → Base Color (never Alpha — P5 cutouts punch holes) |
| No clips / every GAP in one FBX | One giant take, or Rest Pose only | `bake_anim=False`, or `bake_anim_use_all_actions` with every FIELD GAP loaded | Single export bakes actions; **batch** writes `角色_BF251.fbx`, `角色_BF253.fbx`, … |
| Blur outline as geometry | Extra meshes, stretching silhouettes | `Blur*` toon outlines exported as real meshes | Hidden during export (`use_visible=True`) |
| Mesh/bone name clash | Wrong node, missing skin | FBX cannot store a mesh and a bone with the same name | Temporary `{name}__mesh` rename |
| `gfdDefaultMat0` empty | 4 helper slots with no map | Elbow/knee helper meshes have no diffuse in GFS | Not a bug; leave them |

Verified on 芳泽霞: current exporter `Kasumi.fbx` has 21/25 textured slots, 4 BASE clips, uniform scale 1. The original-plugin `芳泽霞_BF251.fbx` had 0 textures and skeleton scale 100 (world size only matched because mesh was 0.01×).

## Plugin Usage
BlenderToolsForGFS makes a few idiomatic choices, such as, but not limited to:
- UV maps must be named UV0 through to UV6, in order to preserve texture coordinate animations.
- Cameras and Lights are attached to bones using `ChildOf` constraints.
- Blend Animations must be split into two Actions - one for Translations and Rotations, and one for Scales.

**You should read the documentation if you need to understand the idiomatic choices used to export data. You can use imported files as a reference to see how data is imported.** There are many opportunites to open the documentation from within Blender by clicking the `How to Use` buttons on many of the data properties panels added by the plugin.

All data from the GFS or GAP file should be preserved from import to export, but not all of it will be represented in Blender. Most of the this data is stored as raw byte data on hidden properties inaccessible to the user. Use dedicated software such as [GFD Studio](https://github.com/tge-was-taken/GFD-Studio) to edit data the plugin is not capable of making accessible to the user.

## Limitations
- Camera aspect ratios are not displayed in Blender.
- Most aspects of Lights are not displayed in Blender.
- Materials are only implemented as using the Diffuse Texture. All Material data is imported, but most is not rendered in order to not be misleading.
- Physics bones are not previewable.
- EPL data is not displayed.
- All animations other than Node/Bone animations are not displayed.

## Future Development
The highest-priority features are, in order of importance:
1) A custom Material Node that faithfully reproduces the material rendering.
2) Import and editability of material, camera, and morph animations.
3) Import, manipulation, and export of submodels in EPL data.
4) Import, manupulation, and export of the model data in EPL files.

## Acknowledgements
This is a Blender importer for the GFS file format. The GFS format code has been heavily derived from [GFD Studio](https://github.com/tge-was-taken/GFD-Studio), tge's [3DS Max importer](https://github.com/tge-was-taken/GFD-Studio/tree/master/Resources/GfdImporter), and the [010 Editor templates](https://github.com/CherryCreamSoda/010-Editor-Templates/blob/master/templates/p5_gfd.bt). Deep thanks are given to all those who have contributed to the understanding of the format.

Additionally, thanks to CherryCreamSoda, DeathChaos, DniweTamp, A Mudkip, ShrineFox, and tge for providing feedback on the plugin development and for assisting with the comprehension of aspects of the GFS file format.

## Supported Formats
The status of the code is tabulated for the different filetypes and versions given in the sections below, with the following keys:

| Key | Status | Definition |
| :---: | :---: | :--- |
|✔️| Supported | Import/export is production-ready .|
|🟡| Partial Support | An incomplete, but partially usable import/export operation exists.|
|❌| Not supported | Insufficient code exists for useful import/export. |

### GMD

| Version | Present In | Import | Export | Notes |
|:---:|:---:|:---:|:---:|:---:|
| TBC | Persona 3 Dancing | 🟡 | 🟡 | (1) |
| TBC | Persona 4 Dancing | 🟡 | 🟡 | (1) |
| TBC | Persona 5 Dancing | 🟡 | 🟡 | (1) |
| 0x01104920 - 0x01105100 | Persona 5 Royal (PC) | 🟡 | 🟡 | (1) |
| TBC |Metaphor Refantazio (PC) | 🟡 | 🟡 | (1) |

(1) There are several missing features, noted in [Future Development](#future-development), that would be necessary for full GFS support.
