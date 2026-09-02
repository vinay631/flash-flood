#!/usr/bin/env python3
"""Build static Planet image pairs and an optional UNOSAT hazard layer.

The resulting revision JSON is consumed by update_map_revision.py. Planet
Crisis Response assets are CC BY-NC 4.0 and must not be used commercially.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_CATALOG = "https://data.source.coop/planet/disasterdata/nepal-flash-flood-2026-08-26/catalog.json"
DEFAULT_UNOSAT = "https://ihp-wins.unesco.org/dataset/054c3516-e2b8-49a4-a256-1de4b7431ec6/resource/9f9498b2-4b48-4e83-8c8c-b227d5938a08/download/floodextent_20260826_nepal.zip"
PLANET_ATTRIBUTION = "Imagery © Planet Labs PBC, Planet Crisis Response Program"
PLANET_LICENSE = "CC BY-NC 4.0"
POST_PRIORITY = [
    "pelican-2026-09-01",
    "skysat-2026-08-31",
    "pelican-2026-08-27",
    "skysat-2026-08-27",
    "planetscope-2026-08-28",
    "planetscope-2026-08-26",
]
PRE_PRIORITY = ["planetscope-2026-05-27"]


class BuildError(RuntimeError):
    pass


def dependencies():
    try:
        import geopandas as gpd
        import numpy as np
        import pystac
        import rasterio
        from PIL import Image
        from pyproj import Transformer
        from rasterio.enums import Resampling
        from rasterio.warp import transform as transform_coords
        from rasterio.windows import from_bounds
        from shapely.geometry import Point, mapping, shape
        from shapely.ops import transform as transform_geometry
        from shapely.ops import unary_union
    except ImportError as exc:
        raise BuildError(
            f"missing dependency {exc.name!r}; install requirements-map-assets.txt"
        ) from exc
    return {
        "gpd": gpd,
        "np": np,
        "pystac": pystac,
        "rasterio": rasterio,
        "Image": Image,
        "Transformer": Transformer,
        "Resampling": Resampling,
        "from_bounds": from_bounds,
        "transform_coords": transform_coords,
        "Point": Point,
        "mapping": mapping,
        "shape": shape,
        "transform_geometry": transform_geometry,
        "unary_union": unary_union,
    }


def slug(value: str, index: int) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return f"{index:03d}-{cleaned[:60] or 'school'}"


def collection_name(item: Any) -> str:
    return str(item.collection_id or "").lower()


def item_datetime(item: Any) -> dt.datetime:
    value = item.datetime or item.common_metadata.start_datetime
    if value is None:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return value


def reported_cloud(item: Any) -> float:
    properties = item.properties
    for key in ("clear_percent", "clear_percent_100", "usable_data"):
        if properties.get(key) is not None:
            value = float(properties[key])
            return max(0.0, 100.0 - value)
    return float(properties.get("eo:cloud_cover", properties.get("cloud_cover", 100.0)))


def covers(item: Any, lon: float, lat: float, dep: dict) -> bool:
    if not item.geometry:
        return False
    point = dep["Point"](lon, lat)
    return dep["shape"](item.geometry).buffer(1e-9).covers(point)


def choose_item(items: list[Any], school: dict, priority: list[str], dep: dict) -> Any | None:
    candidates = [i for i in items if covers(i, school["lon"], school["lat"], dep) and "visual" in i.assets]
    if not candidates:
        return None

    def rank(item: Any) -> tuple:
        collection = collection_name(item)
        try:
            priority_index = priority.index(collection)
        except ValueError:
            priority_index = len(priority) + 1
        quality = 0 if str(item.properties.get("quality_category", "")).lower() == "standard" else 1
        return (priority_index, quality, reported_cloud(item), -item_datetime(item).timestamp())

    return min(candidates, key=rank)


def stretch_rgb(array: Any, dep: dict) -> Any:
    np = dep["np"]
    rgb = np.moveaxis(array[:3], 0, -1).astype("float32")
    output = np.zeros(rgb.shape, dtype="uint8")
    valid = np.isfinite(rgb) & (rgb > 0)
    for band in range(3):
        values = rgb[..., band][valid[..., band]]
        if values.size == 0:
            continue
        low, high = np.percentile(values, (2, 98))
        if high <= low:
            high = low + 1
        output[..., band] = np.clip((rgb[..., band] - low) * 255 / (high - low), 0, 255).astype("uint8")
    return output


def render_crop(item: Any, school: dict, destination: Path, context_m: float, pixels: int, dep: dict) -> dict:
    rasterio = dep["rasterio"]
    href = item.assets["visual"].href
    with rasterio.Env(
        GDAL_HTTP_MAX_RETRY="5",
        GDAL_HTTP_RETRY_DELAY="2",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF",
    ):
        with rasterio.open(href) as source:
            if source.count < 3:
                raise BuildError(f"{item.id}: visual asset has fewer than three bands")
            if source.crs is None:
                raise BuildError(f"{item.id}: visual asset has no coordinate reference system")
            xs, ys = dep["transform_coords"]("EPSG:4326", source.crs, [school["lon"]], [school["lat"]])
            x, y = xs[0], ys[0]
            half = context_m / 2
            if source.crs.is_geographic:
                half_y = half / 111_320
                half_x = half / (111_320 * max(math.cos(math.radians(school["lat"])), 0.1))
            else:
                half_x = half_y = half
            window = dep["from_bounds"](x - half_x, y - half_y, x + half_x, y + half_y, source.transform)
            data = source.read(
                [1, 2, 3],
                window=window,
                out_shape=(3, pixels, pixels),
                boundless=True,
                fill_value=0,
                resampling=dep["Resampling"].bilinear,
            )
    rgb = stretch_rgb(data, dep)
    if not rgb.any():
        raise BuildError(f"{item.id}: crop contains no image data")
    destination.parent.mkdir(parents=True, exist_ok=True)
    dep["Image"].fromarray(rgb, mode="RGB").save(destination, "WEBP", quality=88, method=6)
    gsd = item.properties.get("gsd") or item.common_metadata.gsd
    if isinstance(gsd, (list, tuple)):
        gsd = min(gsd) if gsd else None
    acquired = item_datetime(item).date().isoformat()
    sensor = item.properties.get("platform") or item.properties.get("constellation") or collection_name(item).split("-")[0].title()
    if isinstance(sensor, (list, tuple)):
        sensor = ", ".join(map(str, sensor))
    return {
        "src": destination.as_posix(),
        "acquired": acquired,
        "sensor": str(sensor),
        "gsd_m": round(float(gsd), 2) if gsd is not None else None,
        "scene_id": item.id,
        "cloud_percent_reported": round(reported_cloud(item), 1),
        "attribution": PLANET_ATTRIBUTION,
        "license": PLANET_LICENSE,
        "source": href,
    }


def local_source_path(destination: Path, site_root: Path) -> str:
    try:
        return destination.resolve().relative_to(site_root.resolve()).as_posix()
    except ValueError as exc:
        raise BuildError("asset directory must be inside --site-root so the website can serve it") from exc


def load_hazard(source: str, dep: dict) -> tuple[dict, Any]:
    source_path = Path(source)
    temporary = None
    if not source_path.exists():
        temporary = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        temporary.close()
        try:
            urllib.request.urlretrieve(source, temporary.name)
        except Exception as exc:
            Path(temporary.name).unlink(missing_ok=True)
            raise BuildError(f"could not download UNOSAT data: {exc}") from exc
        source_path = Path(temporary.name)
    try:
        frame = dep["gpd"].read_file(f"zip://{source_path.resolve()}")
        if frame.empty:
            raise BuildError("UNOSAT dataset contains no features")
        if frame.crs is None:
            frame = frame.set_crs("EPSG:32645")
        metric = frame.to_crs("EPSG:32645")
        union_metric = dep["unary_union"](metric.geometry).buffer(0)
        transform = dep["Transformer"].from_crs("EPSG:32645", "EPSG:4326", always_xy=True).transform
        union_wgs84 = dep["transform_geometry"](transform, union_metric.simplify(5, preserve_topology=True))
        return dep["mapping"](union_wgs84), union_metric
    finally:
        if temporary:
            Path(temporary.name).unlink(missing_ok=True)


def add_exposure(school: dict, union_metric: Any, dep: dict) -> None:
    transform = dep["Transformer"].from_crs("EPSG:4326", "EPSG:32645", always_xy=True).transform
    point = dep["transform_geometry"](transform, dep["Point"](school["lon"], school["lat"]))
    distance = float(union_metric.distance(point))
    school["exposure"] = {
        "within_observed_extent": bool(union_metric.covers(point)),
        "distance_to_observed_extent_m": round(distance, 1),
        "method": "point-to-UNOSAT-mudflow-polygon",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision_file", type=Path)
    parser.add_argument("--output-revision", type=Path, default=Path("map-revision.built.json"))
    parser.add_argument("--asset-dir", type=Path, default=Path("imagery"))
    parser.add_argument("--site-root", type=Path, default=Path.cwd())
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    parser.add_argument("--unosat", default=DEFAULT_UNOSAT, help="UNOSAT ZIP path or URL")
    parser.add_argument("--skip-hazard", action="store_true")
    parser.add_argument("--context-metres", type=float, default=1500)
    parser.add_argument("--pixels", type=int, default=1400)
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--revision-number", type=int)
    args = parser.parse_args()

    try:
        dep = dependencies()
        revision = json.loads(args.revision_file.read_text(encoding="utf-8"))
        schools = revision.get("schools")
        if not isinstance(schools, list) or not schools:
            raise BuildError("revision must contain at least one school record")
        catalog = dep["pystac"].Catalog.from_file(args.catalog)
        items = list(catalog.get_all_items())
        if not items:
            raise BuildError("Planet STAC catalog returned no imagery")

        union_metric = None
        if not args.skip_hazard:
            geometry, union_metric = load_hazard(args.unosat, dep)
            revision["hazard"] = {
                "label": "UNOSAT satellite-detected mudflow and rockflow extent",
                "note": "Derived from PlanetScope (26 Aug) and Sentinel-2 (27 Aug); preliminary and not field validated.",
                "observed_at": "2026-08-26/2026-08-27",
                "source": "https://ihp-wins.unesco.org/en/dataset/nepal-satellite-detected-mudflow-and-rockflow-extent-nepal",
                "geojson": {"type": "Feature", "properties": {"provider": "UNOSAT"}, "geometry": geometry},
            }

        failures = []
        asset_dir = args.asset_dir.resolve()
        site_root = args.site_root.resolve()
        for index, school in enumerate(schools, start=1):
            name = str(school.get("name") or f"school-{index}")
            base = slug(name, index)
            before_item = choose_item(items, school, PRE_PRIORITY, dep)
            after_item = choose_item(items, school, POST_PRIORITY, dep)
            if not before_item or not after_item:
                failures.append(f"{name}: no covering {'before' if not before_item else 'after'} scene")
                continue
            try:
                before_path = asset_dir / f"{base}-before.webp"
                after_path = asset_dir / f"{base}-after.webp"
                before = render_crop(before_item, school, before_path, args.context_metres, args.pixels, dep)
                after = render_crop(after_item, school, after_path, args.context_metres, args.pixels, dep)
                before["src"] = local_source_path(before_path, site_root)
                after["src"] = local_source_path(after_path, site_root)
                school["imagery"] = {"before": before, "after": after, "context_metres": args.context_metres}
                if union_metric is not None:
                    add_exposure(school, union_metric, dep)
            except Exception as exc:
                failures.append(f"{name}: {exc}")

        if failures and not args.allow_missing:
            raise BuildError("asset build incomplete:\n  - " + "\n  - ".join(failures))
        revision["osm_live"] = False
        revision["revision"] = args.revision_number or int(revision.get("revision", 0)) + 1
        revision["updated_at"] = dt.date.today().isoformat()
        revision.setdefault("imagery", {}).update({
            "catalog_url": args.catalog,
            "license": PLANET_LICENSE,
            "attribution": PLANET_ATTRIBUTION,
            "build_context_metres": args.context_metres,
            "build_pixels": args.pixels,
        })
        revision["asset_build"] = {
            "status": "complete" if not failures else "partial",
            "generated_pairs": sum(1 for school in schools if school.get("imagery")),
            "missing_pairs": failures,
        }
        args.output_revision.write_text(json.dumps(revision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(revision["asset_build"], indent=2))
        return 0
    except (BuildError, OSError, json.JSONDecodeError) as exc:
        print(f"asset build failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
