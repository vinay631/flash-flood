# Nepal flood map asset pipeline

The website now renders static image pairs and an embedded UNOSAT hazard polygon. It makes no browser-side satellite API calls.

## Build

Use Python 3.11 or newer in the directory containing the website and scripts.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-map-assets.txt

python build_map_assets.py map-revision.example.json \
  --asset-dir imagery \
  --site-root . \
  --output-revision map-revision.built.json

python update_map_revision.py map-revision.built.json --check
python update_map_revision.py map-revision.built.json
python -m http.server 8080
```

Open `http://localhost:8080/nepal-flood-story.html`. Do not open the HTML through a `file://` URL when testing deployment behavior.

## What the build does

1. Reads school locations from the revision JSON.
2. Traverses Planet Crisis Response’s public STAC catalog.
3. Chooses a covering pre-event and post-event scene for each school.
4. Reads only a 1.5 km COG window and exports a 1400×1400 WebP crop.
5. Downloads and embeds UNOSAT’s satellite-detected mudflow/rockflow geometry.
6. Calculates each school’s distance to the observed extent.
7. Writes a new revision with static asset paths and full acquisition metadata.

The build fails if any school lacks a valid pair. Use `--allow-missing` only for an explicitly partial publication. Use `--skip-hazard` when a previously reviewed UNOSAT geometry should be retained manually.

## Data and licensing

- Planet Crisis Response imagery is **CC BY-NC 4.0**. It cannot be used for a commercial publication without another license.
- Display: `Imagery © Planet Labs PBC, Planet Crisis Response Program`.
- The UNOSAT extent is preliminary and has not been field validated.
- A school’s `status` must come from an authoritative field or Education Cluster assessment. Satellite exposure does not assign destruction status.

Source catalog: <https://source.coop/planet/disasterdata/nepal-flash-flood-2026-08-26>

UNOSAT dataset: <https://ihp-wins.unesco.org/en/dataset/nepal-satellite-detected-mudflow-and-rockflow-extent-nepal>
