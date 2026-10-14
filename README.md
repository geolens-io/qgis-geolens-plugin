# GeoLens for QGIS

A QGIS plugin for browsing and using datasets from a self-hosted GeoLens server. It brings the [GeoLens](https://github.com/geolens-io/geolens) catalog workflow from [GeoLibre](https://github.com/opengeos/GeoLibre) into QGIS.

This is a community project and is not affiliated with, or endorsed by, any GeoLens vendor.

## Features

- Connect to any GeoLens server. Private data uses an API key kept in the QGIS authentication database, or pasted for one session.
- Search the catalog and inspect dataset descriptions, types, and feature counts.
- Add vector datasets as signed Mapbox Vector Tile layers with automatic token renewal.
- Add public raster datasets as server-rendered XYZ tile layers.
- Load vector datasets through OGC API Features, optionally clipped to the current map extent.
- Open a dataset's GeoLens metadata page.
- Edit loaded feature layers in QGIS and synchronize creates, updates, and deletes back to GeoLens when the server permits editing.
- Use the public GeoLibre datasets server from the built-in server picker.

## Install for development

Clone the repository and link its plugin directory into your active QGIS profile:

```bash
git clone https://github.com/opengeos/qgis-geolens-plugin.git
cd qgis-geolens-plugin
python install.py
```

Restart QGIS, enable **GeoLens** in **Plugins → Manage and Install Plugins**, then open it from **Web → GeoLens** or the toolbar.

The default Linux profile is `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins`. Override it when needed:

```bash
python install.py --plugin-dir /path/to/profile/python/plugins
```

## Usage

1. Enter a GeoLens server URL or choose a sample server.
2. If the catalog or datasets are private, choose an authentication configuration or paste an API key, then click **Connect**. Tick **Save API key in the QGIS authentication database** to keep a pasted key.
3. Search and select a dataset.
4. Choose **Add tiles** for scalable viewing, or **Add features** for attributes and editing.
5. For an editable feature layer, commit QGIS edits, select that layer, and click **Sync selected GeoLens layer edits**.

![](https://github.com/user-attachments/assets/3842764d-6e33-4b2b-8cb5-d56d7db2bfca)

GeoLens writes are per-feature and are not transactional. The plugin reports partial failures and requires confirmation before deleting server features.

A saved API key is an API Header configuration (`X-Api-Key`) in the QGIS
authentication database, encrypted with the QGIS master password. Earlier
versions could keep the key in plain text in the profile settings; opening the
GeoLens panel moves such a key into the authentication database and deletes
the plain-text copy. If that move fails or you cancel the master password
prompt, the key stays where it was and the panel tries again in your next QGIS
session. No key
is written to a layer source or a project file.

Private vector and raster tiles load with a short-lived signed token that the
plugin renews while the layer is open. No API key is written into a layer
source URL. A saved project keeps the last token, which expires within minutes,
so a reopened project shows private tiles again once you re-add the layer from
the GeoLens panel.

## Development

The HTTP client has no QGIS dependency, so its tests run with standard Python:

```bash
python -m pytest tests -v
python -m compileall -q geolens_qgis
python package_plugin.py
```

The generated `dist/geolens_qgis-<version>.zip` has the directory layout expected by the QGIS Plugin Manager.

## Project structure

```text
geolens_qgis/
├── dialogs/geolens_dock.py  # Catalog, layer, and edit-sync panel
├── icons/                   # Toolbar and About SVG assets
├── client.py                # Dependency-free GeoLens API client
├── connection.py            # Server, QGIS auth config and network transport
├── plugin.py                # QGIS lifecycle, menu, and toolbar integration
├── metadata.txt
└── LICENSE
tests/                       # API, package, and PyQt6 import tests
install.py                   # Cross-platform development installer
package_plugin.py            # QGIS repository archive builder
.github/workflows/           # CI and release publishing
```

CI mirrors the template with pre-commit, Bandit, and PyQt6 smoke-test jobs.
Publishing a GitHub release builds and attaches the plugin archive; when QGIS
repository credentials are configured, the same workflow uploads it to
plugins.qgis.org.

## License

MIT
