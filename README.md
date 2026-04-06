# Havok IO (Blender 5.0)

A Blender add-on that replaces the archived HavokMax 3ds Max plugin. The port
adds Blender-native import entries for Havok `.hkx`, `.hkt`, `.hka`, `.igz`,
and `.pak` containers, plus configuration options that mirror the original
tool's presets.

> **Status:** The importer now reads Havok 2010.2 pakfiles directly, including Skeleton and animation files. 
> Tested on Blender 5.1 already but not on Blender 4.x

## Installation

1. Download or clone this repository, then zip the repo
2. From Blender **Edit → Preferences → Add-ons → Install from Disk**, select the zipped project
3. Enable **Havok IO** in the add-on list.

## Usage

- **Import:** `File → Import → Havok (.hkx/.hkt/.hka/.igz/.pak)` will unpack
  compressed IGZ payloads, scan PAK containers (zip, tar, or raw blobs) for
  Havok XML, and build an armature plus animation actions. If the Havok payload
  includes mesh geometry, the importer will create static mesh objects even
  without an armature. Selecting a `.pak` now requires picking the **Game
  version** (layout profile) and **Platform** endianness instead of
  auto-guessing every combination; the importer only uses the selected layout.
  A tree of contained entries parsed with the igArchiveExtractor chunk rules
  will update after you choose those options so you can pick an entry or type a
  path manually in **Archive entry**.
- **Presets:** Configure scale and up/forward axes in **Preferences → Add-ons →
  Havok IO** or in the 3D View **Havok** side panel.

### HKX/HKT/HKA support

The add-on expects Havok XML packfiles. If you encounter binary Havok assets,
convert them to XML first (for example with hkxpack) and then import the
resulting XML or IGZ/PAK wrapper.

## Development

The add-on uses Blender's new `manifest.toml` packaging format (introduced for
Blender 4.2+ and forward-compatible with Blender 5.0). Source files live in the
`havok_blender/` package:

- `operators/` contains the import operator and shared preferences.
- `ui/` defines the 3D View toolbar panel.

To validate basic syntax without Blender:

```bash
python -m compileall havok_blender
```

## License

Havok IO is available under the GPL v3 license (see `LICENSE`).
