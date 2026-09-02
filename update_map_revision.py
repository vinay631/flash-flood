#!/usr/bin/env python3
"""Safely apply a structured revision to nepal-flood-story.html."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


REVISION_BLOCK = re.compile(
    r'(<script\s+id="revision-data"\s+type="application/json">\s*)(.*?)(\s*</script>)',
    re.DOTALL,
)


class RevisionError(ValueError):
    """Raised when revision data cannot safely update the map."""


def iso_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RevisionError(f"{field} must be an ISO date string")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise RevisionError(f"{field} must use YYYY-MM-DD: {value!r}") from exc
    return value


def nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RevisionError(f"{field} must be a non-negative integer")
    return value


def coordinate(lat: object, lon: object, field: str) -> tuple[float, float]:
    if not isinstance(lat, (int, float)) or not -90 <= lat <= 90:
        raise RevisionError(f"{field}.lat is outside -90..90")
    if not isinstance(lon, (int, float)) or not -180 <= lon <= 180:
        raise RevisionError(f"{field}.lon is outside -180..180")
    return float(lat), float(lon)


def require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RevisionError(f"{field} must be a non-empty string")
    return value.strip()


def validate_revision(data: object) -> dict:
    if not isinstance(data, dict):
        raise RevisionError("revision file must contain one JSON object")

    required = {"revision", "updated_at", "event_date", "event_label", "assessment_label", "impact", "hydrology", "route", "places", "schools", "imagery", "sources"}
    missing = sorted(required - data.keys())
    if missing:
        raise RevisionError("missing required fields: " + ", ".join(missing))

    nonnegative_int(data["revision"], "revision")
    if data["revision"] < 1:
        raise RevisionError("revision must be at least 1")
    iso_date(data["updated_at"], "updated_at")
    iso_date(data["event_date"], "event_date")
    require_text(data["event_label"], "event_label")
    require_text(data["assessment_label"], "assessment_label")

    impact = data["impact"]
    if not isinstance(impact, dict):
        raise RevisionError("impact must be an object")
    for key in ("damaged_total", "destroyed", "partial", "assessment", "within_1km"):
        nonnegative_int(impact.get(key), f"impact.{key}")
    classified = impact["destroyed"] + impact["partial"] + impact["assessment"]
    if classified != impact["damaged_total"]:
        raise RevisionError(
            "impact counts do not reconcile: destroyed + partial + assessment "
            f"is {classified}, but damaged_total is {impact['damaged_total']}"
        )
    districts = impact.get("affected_districts")
    if not isinstance(districts, list) or not districts:
        raise RevisionError("impact.affected_districts must contain at least one district")
    for index, district in enumerate(districts):
        require_text(district, f"impact.affected_districts[{index}]")

    hydrology = data["hydrology"]
    if not isinstance(hydrology, dict):
        raise RevisionError("hydrology must be an object")
    if not isinstance(hydrology.get("rise_m"), (int, float)) or hydrology["rise_m"] < 0:
        raise RevisionError("hydrology.rise_m must be a non-negative number")
    nonnegative_int(hydrology.get("rise_minutes"), "hydrology.rise_minutes")
    require_text(hydrology.get("station"), "hydrology.station")

    route = data["route"]
    if not isinstance(route, list) or len(route) < 2:
        raise RevisionError("route must contain at least two [lat, lon] points")
    for index, point in enumerate(route):
        if not isinstance(point, list) or len(point) != 2:
            raise RevisionError(f"route[{index}] must be [lat, lon]")
        coordinate(point[0], point[1], f"route[{index}]")

    for collection in ("places", "schools"):
        rows = data[collection]
        if not isinstance(rows, list):
            raise RevisionError(f"{collection} must be an array")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise RevisionError(f"{collection}[{index}] must be an object")
            coordinate(row.get("lat"), row.get("lon"), f"{collection}[{index}]")
            require_text(row.get("name"), f"{collection}[{index}].name")
            if collection == "places":
                require_text(row.get("note"), f"places[{index}].note")
            status = row.get("status")
            if status is not None and status not in {"destroyed", "partial", "assessment", "unverified", "unaffected"}:
                raise RevisionError(f"schools[{index}].status is not recognized: {status!r}")
            exposure = row.get("exposure")
            if exposure is not None:
                if not isinstance(exposure, dict) or not isinstance(exposure.get("within_observed_extent"), bool):
                    raise RevisionError(f"schools[{index}].exposure must include within_observed_extent as a boolean")
                distance = exposure.get("distance_to_observed_extent_m")
                if not isinstance(distance, (int, float)) or distance < 0:
                    raise RevisionError(f"schools[{index}].exposure.distance_to_observed_extent_m must be non-negative")
            image_pair = row.get("imagery")
            if image_pair is not None:
                if not isinstance(image_pair, dict):
                    raise RevisionError(f"schools[{index}].imagery must be an object")
                for phase in ("before", "after"):
                    scene = image_pair.get(phase)
                    if not isinstance(scene, dict):
                        raise RevisionError(f"schools[{index}].imagery.{phase} must be an object")
                    src = require_text(scene.get("src"), f"schools[{index}].imagery.{phase}.src")
                    src_path = Path(src)
                    if src_path.is_absolute() or ".." in src_path.parts or urlparse(src).scheme:
                        raise RevisionError(f"schools[{index}].imagery.{phase}.src must be a safe relative path")
                    iso_date(scene.get("acquired"), f"schools[{index}].imagery.{phase}.acquired")
                    require_text(scene.get("sensor"), f"schools[{index}].imagery.{phase}.sensor")
                    require_text(scene.get("attribution"), f"schools[{index}].imagery.{phase}.attribution")
                    require_text(scene.get("license"), f"schools[{index}].imagery.{phase}.license")

    imagery = data["imagery"]
    if not isinstance(imagery, dict):
        raise RevisionError("imagery must be an object")
    for field in ("before_start", "before_end", "after_start", "after_end"):
        iso_date(imagery.get(field), f"imagery.{field}")
    if imagery["before_start"] > imagery["before_end"] or imagery["after_start"] > imagery["after_end"]:
        raise RevisionError("imagery date windows must run from earlier to later")
    for field in ("catalog_url",):
        if imagery.get(field):
            parsed = urlparse(imagery[field])
            if parsed.scheme != "https" or not parsed.netloc:
                raise RevisionError(f"imagery.{field} must be a valid HTTPS URL")
    for field in ("license", "attribution"):
        if imagery.get(field) is not None:
            require_text(imagery[field], f"imagery.{field}")

    if data.get("osm_live") is not None and not isinstance(data["osm_live"], bool):
        raise RevisionError("osm_live must be a boolean")
    hazard = data.get("hazard")
    if hazard is not None:
        if not isinstance(hazard, dict):
            raise RevisionError("hazard must be null or an object")
        require_text(hazard.get("label"), "hazard.label")
        parsed = urlparse(require_text(hazard.get("source"), "hazard.source"))
        if parsed.scheme != "https" or not parsed.netloc:
            raise RevisionError("hazard.source must be a valid HTTPS URL")
        geojson = hazard.get("geojson")
        if not isinstance(geojson, dict) or geojson.get("type") not in {"Feature", "FeatureCollection"}:
            raise RevisionError("hazard.geojson must be a GeoJSON Feature or FeatureCollection")

    if not isinstance(data["sources"], list) or not data["sources"]:
        raise RevisionError("sources must contain at least one source")
    for index, source in enumerate(data["sources"]):
        if not isinstance(source, dict):
            raise RevisionError(f"sources[{index}] must be an object")
        require_text(source.get("title"), f"sources[{index}].title")
        url = require_text(source.get("url"), f"sources[{index}].url")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RevisionError(f"sources[{index}].url must be a valid HTTPS URL")

    return data


def load_json(path: Path) -> dict:
    try:
        return validate_revision(json.loads(path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise RevisionError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RevisionError(f"invalid JSON in {path}: {exc}") from exc


def current_revision(html: str) -> tuple[int, re.Match[str]]:
    match = REVISION_BLOCK.search(html)
    if not match:
        raise RevisionError('site does not contain <script id="revision-data">')
    try:
        current = json.loads(match.group(2))
        return int(current["revision"]), match
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RevisionError("site contains an unreadable revision block") from exc


def validate_local_assets(data: dict, site_directory: Path) -> None:
    missing = []
    for index, school in enumerate(data.get("schools", [])):
        for phase, scene in (school.get("imagery") or {}).items():
            if phase not in {"before", "after"} or not isinstance(scene, dict) or not scene.get("src"):
                continue
            candidate = site_directory / scene["src"]
            if not candidate.is_file():
                missing.append(f"schools[{index}].imagery.{phase}: {scene['src']}")
    if missing:
        raise RevisionError("referenced static imagery is missing:\n  - " + "\n  - ".join(missing))


def atomic_write(path: Path, content: str, backup: bool) -> Path | None:
    backup_path = path.with_suffix(path.suffix + ".bak") if backup else None
    if backup_path:
        shutil.copy2(path, backup_path)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision_file", type=Path, help="validated revision JSON")
    parser.add_argument("--site", type=Path, default=Path(__file__).with_name("nepal-flood-story.html"))
    parser.add_argument("--output", type=Path, help="write a separate HTML file instead of updating --site")
    parser.add_argument("--check", action="store_true", help="validate only; do not write")
    parser.add_argument("--dry-run", action="store_true", help="show the proposed update; do not write")
    parser.add_argument("--force", action="store_true", help="allow a revision number that is not newer")
    parser.add_argument("--no-backup", action="store_true", help="do not keep SITE.html.bak for in-place updates")
    parser.add_argument("--allow-missing-assets", action="store_true", help="allow revision references to absent static images")
    args = parser.parse_args()

    try:
        incoming = load_json(args.revision_file)
        if not args.allow_missing_assets:
            validate_local_assets(incoming, args.site.parent)
        html = args.site.read_text(encoding="utf-8")
        existing_number, match = current_revision(html)
        if incoming["revision"] <= existing_number and not args.force:
            raise RevisionError(
                f"incoming revision {incoming['revision']} is not newer than site revision {existing_number}; "
                "use --force only after verifying the source"
            )
        target = args.output or args.site
        summary = {
            "site": str(args.site),
            "output": str(target),
            "from_revision": existing_number,
            "to_revision": incoming["revision"],
            "updated_at": incoming["updated_at"],
            "schools_damaged": incoming["impact"]["damaged_total"],
            "mapped_school_records": len(incoming["schools"]),
        }
        if args.check or args.dry_run:
            print(json.dumps(summary, indent=2))
            return 0

        serialized = json.dumps(incoming, ensure_ascii=False, indent=2).replace("</", "<\\/")
        updated = html[: match.start(2)] + serialized + html[match.end(2) :]
        backup = atomic_write(target, updated, backup=(target == args.site and not args.no_backup))
        summary["status"] = "updated"
        summary["backup"] = str(backup) if backup else None
        print(json.dumps(summary, indent=2))
        return 0
    except (RevisionError, OSError) as exc:
        print(f"update failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
