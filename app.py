
import json
import math
from collections import Counter
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="Cyprus DLS Site Explorer V10", version="1.0.0")

DLS_MAPSERVER = "https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer"
PARCEL_QUERY = f"{DLS_MAPSERVER}/0/query"
GENERAL_IDENTIFY = "https://eservices.dls.moi.gov.cy/Services/Rest/Info/GeneralParcelIdentify"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

GEOCODE_CACHE: dict[str, list[dict[str, Any]]] = {}

# Confirmed / observed DLS map layers from the official viewer.
SPECIAL_LAYERS = {
    28: "Buildings",
    30: "Contour Lines 1993",
    31: "Coast Protection Zone",
    32: "State Land",
    36: "Surveyed Parcels",
}


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/geocode")
async def geocode(q: str = Query(min_length=3, max_length=200)):
    key = q.strip().casefold()
    if key in GEOCODE_CACHE:
        return {"results": GEOCODE_CACHE[key]}

    params = {
        "q": f"{q.strip()}, Cyprus",
        "format": "jsonv2",
        "limit": 5,
        "countrycodes": "cy",
    }
    headers = {
        "User-Agent": "CyprusDLSSiteExplorer/1.0",
        "Accept-Language": "en,el;q=0.8",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(NOMINATIM, params=params, headers=headers)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Address search failed.")

    results = [
        {
            "display_name": x.get("display_name"),
            "lat": float(x["lat"]),
            "lon": float(x["lon"]),
        }
        for x in r.json()
        if x.get("lat") and x.get("lon")
    ]
    GEOCODE_CACHE[key] = results
    return {"results": results}


async def get_parcel_at_point(lat: float, lon: float):
    params = {
        "f": "geojson",
        "where": "1=1",
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "resultRecordCount": 5,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(PARCEL_QUERY, params=params)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="DLS parcel query failed.")

    data = r.json()
    features = data.get("features", [])
    if not features:
        raise HTTPException(status_code=404, detail="No DLS parcel found at that point.")
    return features[0]


async def get_general_identify(subproperty_id: int):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://eservices.dls.moi.gov.cy/",
        "User-Agent": "Mozilla/5.0 CyprusDLSSiteExplorer/1.0",
    }

    async with httpx.AsyncClient(timeout=75.0, follow_redirects=True) as client:
        r = await client.get(
            GENERAL_IDENTIFY,
            params={"subPropertyId": subproperty_id},
            headers=headers,
        )

    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"DLS GeneralParcelIdentify failed ({r.status_code}).",
        )

    try:
        return r.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="DLS GeneralParcelIdentify returned invalid JSON.",
        )


def clean_text(v):
    return v.strip() if isinstance(v, str) else v


def as_percent(v):
    if v in (None, ""):
        return None
    try:
        x = float(v)
        return round(x * 100, 2) if abs(x) <= 5 else round(x, 2)
    except Exception:
        return v


def pick_parcel_record(records, parcel_id):
    for x in records:
        if x.get("PrParcelId") == parcel_id and x.get("PropertyTypeName") == "Parcel":
            return x
    for x in records:
        if x.get("PropertyTypeName") == "Parcel":
            return x
    return records[0] if records else None


def parse_zone(z, link=None):
    if not z:
        return None

    affected = link.get("PrAffectedExtent") if link else None
    total = link.get("PrTotalExtent") if link else None
    overlap = None
    try:
        if affected is not None and total not in (None, 0):
            overlap = round(float(affected) / float(total) * 100, 2)
    except Exception:
        pass

    return {
        "zone": clean_text(z.get("PrName")),
        "density_percent": as_percent(z.get("PrDensityRateQty")),
        "coverage_percent": as_percent(z.get("PrCoverageRate")),
        "max_floors": z.get("PrStoreyNoQty"),
        "max_height_m": z.get("PrHeightMSR"),
        "remarks": clean_text(z.get("PrRemarkDesc")),
        "description_en": clean_text(z.get("PrNameEn")),
        "description_gr": clean_text(z.get("PrNameGr")),
        "affected_extent": affected,
        "total_extent": total,
        "overlap_percent": overlap,
    }


def haversine_m(lon1, lat1, lon2, lat2):
    r = 6371008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def polygon_geometry_metrics(feature):
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []

    if geom.get("type") != "Polygon" or not coords:
        return {}

    outer = max(coords, key=len)
    if len(outer) < 2:
        return {}

    edge_lengths = []
    perimeter = 0.0
    for a, b in zip(outer, outer[1:]):
        d = haversine_m(a[0], a[1], b[0], b[1])
        edge_lengths.append(d)
        perimeter += d

    lons = [p[0] for p in outer]
    lats = [p[1] for p in outer]

    longest = max(edge_lengths) if edge_lengths else None
    shortest = min(edge_lengths) if edge_lengths else None

    orientation_deg = None
    orientation_label = None
    if edge_lengths:
        idx = edge_lengths.index(longest)
        a = outer[idx]
        b = outer[idx + 1]
        y = math.sin(math.radians(b[0] - a[0])) * math.cos(math.radians(b[1]))
        x = (
            math.cos(math.radians(a[1])) * math.sin(math.radians(b[1]))
            - math.sin(math.radians(a[1]))
            * math.cos(math.radians(b[1]))
            * math.cos(math.radians(b[0] - a[0]))
        )
        bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
        dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        orientation_deg = round(bearing, 1)
        orientation_label = dirs[int((bearing + 22.5) // 45) % 8]

    return {
        "approx_perimeter_m": round(perimeter, 2),
        "longest_edge_m": round(longest, 2) if longest is not None else None,
        "shortest_edge_m": round(shortest, 2) if shortest is not None else None,
        "centroid_lat": round(sum(lats) / len(lats), 7),
        "centroid_lon": round(sum(lons) / len(lons), 7),
        "longest_edge_orientation_deg": orientation_deg,
        "longest_edge_orientation": orientation_label,
    }


def geojson_to_esri_polygon(feature):
    geom = feature.get("geometry") or {}
    if geom.get("type") != "Polygon":
        return None
    return {
        "rings": geom.get("coordinates") or [],
        "spatialReference": {"wkid": 4326},
    }


async def query_layer_intersections(layer_id: int, parcel_feature: dict):
    esri_geom = geojson_to_esri_polygon(parcel_feature)
    if not esri_geom:
        return {"ok": False, "error": "Unsupported parcel geometry"}

    url = f"{DLS_MAPSERVER}/{layer_id}/query"
    params = {
        "f": "json",
        "where": "1=1",
        "geometry": json.dumps(esri_geom),
        "geometryType": "esriGeometryPolygon",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": 1000,
    }

    try:
        async with httpx.AsyncClient(timeout=40.0) as client:
            r = await client.get(url, params=params)

        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}

        data = r.json()
        if "error" in data:
            return {"ok": False, "error": data["error"]}

        return {
            "ok": True,
            "features": data.get("features", []),
            "exceeded_transfer_limit": data.get("exceededTransferLimit", False),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/site")
async def site(
    lat: float = Query(ge=34.0, le=36.0),
    lon: float = Query(ge=31.0, le=35.0),
):
    parcel_feature = await get_parcel_at_point(lat, lon)
    map_props = parcel_feature.get("properties", {})

    sbpi = map_props.get("SBPI_ID_NO")
    if sbpi is None:
        raise HTTPException(status_code=502, detail="DLS parcel did not return SBPI_ID_NO.")

    try:
        sbpi = int(sbpi)
    except Exception:
        raise HTTPException(status_code=502, detail=f"Unexpected SBPI_ID_NO: {sbpi}")

    records = await get_general_identify(sbpi)
    if not isinstance(records, list) or not records:
        raise HTTPException(status_code=502, detail="DLS Identify returned no records.")

    parcel = pick_parcel_record(records, sbpi)
    if not parcel:
        raise HTTPException(status_code=502, detail="Main parcel record could not be identified.")

    zones = []
    for link in parcel.get("ParcelPlanZones") or []:
        parsed = parse_zone(link.get("PrPlanningZone"), link)
        if parsed:
            zones.append(parsed)
    if not zones:
        parsed = parse_zone(parcel.get("PrPlanningZone"))
        if parsed:
            zones.append(parsed)

    related = []
    type_counter = Counter()
    enclosed_vals, covered_vals, uncovered_vals = [], [], []

    for rec in records:
        if rec is parcel:
            continue
        subitems = rec.get("PrPropertySubproperty") or []
        sub = subitems[0] if subitems else {}

        kind = clean_text(rec.get("SubPropertyKindName"))
        prop_type = clean_text(rec.get("PropertyTypeName"))
        type_counter[kind or prop_type or "Other"] += 1

        enclosed = sub.get("PrEnclosedExtent")
        covered = sub.get("PrCoveredExtent")
        uncovered = sub.get("PrUncoveredExtent")
        enclosed_vals.append(enclosed)
        covered_vals.append(covered)
        uncovered_vals.append(uncovered)

        related.append({
            "property_type": prop_type,
            "kind": kind,
            "registration_block": rec.get("PrRegistrationBlock"),
            "registration_no": clean_text(rec.get("PrRegistrationNo")),
            "price_2021": rec.get("PrPriceBase2"),
            "price_2018": rec.get("PrPriceBase1"),
            "price_1980": rec.get("PrPriceBase3"),
            "unit_floor_no": sub.get("UnitFloorNo"),
            "plan_no": clean_text(sub.get("PlanNo")),
            "enclosed_extent": enclosed,
            "covered_extent": covered,
            "uncovered_extent": uncovered,
            "is_legal": sub.get("PrIsLegal"),
        })

    def safe_sum(values):
        nums = []
        for v in values:
            try:
                if v not in (None, ""):
                    nums.append(float(v))
            except Exception:
                pass
        return round(sum(nums), 2) if nums else None

    parcel_area = parcel.get("PrParcelExtent")
    max_floor_area = None
    max_ground_coverage = None

    try:
        area = float(parcel_area)
        if zones:
            if len(zones) == 1:
                z = zones[0]
                if z.get("density_percent") is not None:
                    max_floor_area = round(area * float(z["density_percent"]) / 100, 2)
                if z.get("coverage_percent") is not None:
                    max_ground_coverage = round(area * float(z["coverage_percent"]) / 100, 2)
            else:
                floor_total = 0.0
                cov_total = 0.0
                floor_ok = cov_ok = False
                for z in zones:
                    overlap = z.get("overlap_percent")
                    if overlap is None:
                        continue
                    affected_area = area * float(overlap) / 100
                    if z.get("density_percent") is not None:
                        floor_total += affected_area * float(z["density_percent"]) / 100
                        floor_ok = True
                    if z.get("coverage_percent") is not None:
                        cov_total += affected_area * float(z["coverage_percent"]) / 100
                        cov_ok = True
                if floor_ok:
                    max_floor_area = round(floor_total, 2)
                if cov_ok:
                    max_ground_coverage = round(cov_total, 2)
    except Exception:
        pass

    value_2021 = parcel.get("PrPriceBase2")
    value_2018 = parcel.get("PrPriceBase1")
    valuation_change_percent = None
    try:
        if value_2021 is not None and value_2018 not in (None, 0):
            valuation_change_percent = round(
                (float(value_2021) - float(value_2018)) / float(value_2018) * 100,
                2,
            )
    except Exception:
        pass

    geometry_metrics = polygon_geometry_metrics(parcel_feature)

    spatial_checks = {}
    for layer_id, layer_name in SPECIAL_LAYERS.items():
        result = await query_layer_intersections(layer_id, parcel_feature)
        spatial_checks[str(layer_id)] = {"layer_name": layer_name, **result}

    buildings = []
    bcheck = spatial_checks.get("28", {})
    if bcheck.get("ok"):
        for f in bcheck.get("features", []):
            a = f.get("attributes", {})
            buildings.append({
                "object_id": a.get("Object ID") or a.get("OBJECTID"),
                "building_code": a.get("BLDG_CODE"),
                "building_description": clean_text(a.get("BLDG_DESC")),
            })

    contour_values = []
    ccheck = spatial_checks.get("30", {})
    if ccheck.get("ok"):
        for f in ccheck.get("features", []):
            a = f.get("attributes", {})
            val = a.get("Elevation")
            if val is not None:
                try:
                    contour_values.append(float(val))
                except Exception:
                    pass

    warnings = []
    if len(zones) > 1:
        warnings.append("Parcel is affected by multiple planning zones.")
    if any(z.get("remarks") for z in zones):
        warnings.append("One or more planning-zone remarks apply.")
    if related:
        warnings.append(f"Parcel has {len(related)} related registered properties or units.")
    if buildings:
        warnings.append(f"{len(buildings)} DLS building feature(s) intersect the parcel.")
    if bool(parcel.get("PrIsPreserved")):
        warnings.append("Property is marked as preserved.")
    if bool(parcel.get("PrIsAncient")):
        warnings.append("Property is marked as ancient.")
    if bool(parcel.get("PrIsCommonProperty")):
        warnings.append("Property is marked as common property.")

    for lid in ("31", "32"):
        check = spatial_checks.get(lid, {})
        if check.get("ok") and check.get("features"):
            warnings.append(f"Parcel intersects {check['layer_name']}.")

    parcel_summary = {
        "parcel_number": clean_text(parcel.get("PrParcelNo")),
        "registration_number": clean_text(parcel.get("PrRegistrationNo")),
        "district": clean_text(parcel.get("PrDistrictNameEn") or parcel.get("DistrictName")),
        "municipality": clean_text(parcel.get("PrMunicipalityNameEn") or parcel.get("MunicipalityName")),
        "quarter": clean_text(parcel.get("PrQuarterNameEn") or parcel.get("QuarterName")),
        "sheet": clean_text(parcel.get("PrSheetValue")),
        "plan": clean_text(parcel.get("PrPlanValue")),
        "block": clean_text(parcel.get("PrBlockValue")),
        "scale": clean_text(parcel.get("PrScaleValue")),
        "postal_code": clean_text(parcel.get("PrPostalCode")),
        "house_no": parcel.get("PrHouseNo"),
        "parcel_extent_m2": parcel_area,
        "map_geometry_extent_m2": map_props.get("Parcel Extend") or map_props.get("SHAPE.STArea()"),
        "price_2021": value_2021,
        "price_2018": value_2018,
        "price_1980": parcel.get("PrPriceBase3"),
        "valuation_change_percent": valuation_change_percent,
        "is_preserved": bool(parcel.get("PrIsPreserved")),
        "is_ancient": bool(parcel.get("PrIsAncient")),
        "is_common_property": bool(parcel.get("PrIsCommonProperty")),
    }

    return {
        "parcel_feature": parcel_feature,
        "parcel": parcel_summary,
        "planning_zones": zones,
        "development_potential": {
            "theoretical_max_floor_area_m2": max_floor_area,
            "theoretical_max_ground_coverage_m2": max_ground_coverage,
        },
        "geometry_metrics": geometry_metrics,
        "building_summary": {
            "count": len(buildings),
            "features": buildings,
        },
        "contour_summary": {
            "count": len(contour_values),
            "min_elevation_m": min(contour_values) if contour_values else None,
            "max_elevation_m": max(contour_values) if contour_values else None,
            "elevation_range_m": round(max(contour_values) - min(contour_values), 2) if len(contour_values) >= 2 else None,
            "values_m": sorted(set(contour_values)),
        },
        "spatial_checks": spatial_checks,
        "registration_summary": {
            "total_related_records": len(related),
            "by_type": dict(type_counter),
            "total_enclosed_extent_m2": safe_sum(enclosed_vals),
            "total_covered_extent_m2": safe_sum(covered_vals),
            "total_uncovered_extent_m2": safe_sum(uncovered_vals),
        },
        "related_properties": related,
        "warnings": warnings,
    }


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyprus DLS Site Explorer V10</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--ink:#17211b;--green:#173f2b;--muted:#68726c;--line:#dfe5e0;--bg:#f4f5f3;--card:#f7f8f7;--warn:#fff4dc}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}
header{min-height:72px;padding:14px 20px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}
h1{font-size:20px;margin:2px 0 0}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.14em;color:var(--muted)}
.layout{display:grid;grid-template-columns:540px 1fr;height:calc(100vh - 72px)}
aside{background:#fff;border-right:1px solid var(--line);padding:16px;overflow:auto}#map{height:100%}
form{display:flex;gap:8px}.search{flex:1;padding:12px;border:1px solid #ccd4ce;border-radius:11px;font:inherit}
button{border:0;border-radius:11px;background:var(--green);color:#fff;padding:11px 14px;font-weight:750;cursor:pointer}.secondary{background:#edf3ef;color:var(--green)}
.result{width:100%;display:block;margin-top:7px;text-align:left;background:#edf3ef;color:var(--ink)}
.section{margin-top:20px;padding-top:17px;border-top:1px solid var(--line)}.section h2{font-size:16px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.card{background:var(--card);border:1px solid #e5e9e6;border-radius:12px;padding:11px}
.label{font-size:10px;letter-spacing:.07em;text-transform:uppercase;font-weight:800;color:var(--muted)}.value{font-weight:780;margin-top:4px;word-break:break-word}.big{font-size:26px;color:var(--green)}
.zone{border:1px solid var(--line);border-radius:15px;padding:14px;margin-bottom:12px}.zone-title{font-size:31px;font-weight:850;color:var(--green)}
.badge{display:inline-block;margin-top:8px;background:#e8f0ea;color:var(--green);font-size:11px;font-weight:800;padding:5px 8px;border-radius:999px}
.muted{font-size:12px;color:var(--muted);line-height:1.5}.notice{background:#e9f6ec;border:1px solid #bbdec3;border-radius:10px;padding:10px;font-size:12px}
.warning{background:var(--warn);border:1px solid #ead39d;border-radius:10px;padding:10px;font-size:12px;margin-top:7px}
.summary-pills{display:flex;gap:7px;flex-wrap:wrap}.pill{background:#edf3ef;color:var(--green);padding:6px 9px;border-radius:999px;font-size:12px;font-weight:750}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;font-size:12px;min-width:900px}
th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:#f3f5f3;font-size:10px;text-transform:uppercase}
.check{padding:8px 0;border-bottom:1px solid var(--line);font-size:12px}
@media(max-width:900px){.layout{grid-template-columns:1fr;height:auto}#map{height:65vh}}
@media print{.layout{display:block;height:auto}aside{border:0;overflow:visible}#map,form,#results,.secondary{display:none!important}.section{break-inside:avoid}.table-wrap{overflow:visible}table{min-width:0;font-size:9px}}
</style>
</head>
<body>
<header>
<div><div class="eyebrow">DLS SITE INTELLIGENCE REPORT</div><h1>Cyprus Site Explorer V10</h1></div>
<button class="secondary" onclick="window.print()">Print / Save PDF</button>
</header>

<div class="layout">
<aside>
<form id="searchForm"><input id="searchInput" class="search" placeholder="Search address in Cyprus"><button>Search</button></form>
<div id="results"></div>

<section class="section"><h2>Overview</h2><div id="overview" class="muted">Search, zoom in and click a parcel.</div></section>
<section class="section"><h2>Planning</h2><div id="planning" class="muted">No parcel selected.</div></section>
<section class="section"><h2>Development potential</h2><div id="potential" class="muted">No parcel selected.</div></section>
<section class="section"><h2>Parcel geometry</h2><div id="geometry" class="muted">No parcel selected.</div></section>
<section class="section"><h2>Buildings & terrain</h2><div id="terrain" class="muted">No parcel selected.</div></section>
<section class="section"><h2>Spatial checks</h2><div id="spatial" class="muted">No parcel selected.</div></section>
<section class="section"><h2>Warnings</h2><div id="warnings" class="muted">No parcel selected.</div></section>
<section class="section"><h2>DLS General Valuation</h2><div id="valuation" class="muted">No parcel selected.</div></section>
<section class="section"><h2>Registrations on parcel</h2><div id="registrations" class="muted">No parcel selected.</div></section>
<section class="section"><h2>All registered units</h2><div id="units" class="muted">No parcel selected.</div></section>
</aside>
<div id="map"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/esri-leaflet@3.0.15/dist/esri-leaflet.js"></script>
<script>
const DLS="https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer";
const map=L.map("map").setView([35.1264,33.4299],9);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:20,attribution:"&copy; OpenStreetMap contributors"}).addTo(map);
L.esri.dynamicMapLayer({url:DLS,layers:[0],opacity:1,minZoom:15}).addTo(map);

let selected=null;
const $=id=>document.getElementById(id);
const esc=v=>String(v??"—").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
const present=v=>!(v===null||v===undefined||v==="");
function card(label,value,big=false,suffix=""){return `<div class="card"><div class="label">${esc(label)}</div><div class="value ${big?"big":""}">${present(value)?esc(value)+suffix:"—"}</div></div>`}
function money(v){return present(v)?"€"+Number(v).toLocaleString(undefined,{maximumFractionDigits:2}):"—"}

function renderZones(zones){
 if(!zones?.length)return '<div class="muted">No planning-zone data returned.</div>';
 return zones.map(z=>`<div class="zone"><div class="label">Planning zone</div><div class="zone-title">${esc(z.zone)}</div>
 ${present(z.overlap_percent)?`<div class="badge">${esc(z.overlap_percent)}% of parcel</div>`:""}
 <div class="grid" style="margin-top:12px">${card("Density / Δόμηση",z.density_percent,true,"%")}${card("Coverage / Κάλυψη",z.coverage_percent,true,"%")}${card("Maximum floors",z.max_floors,true)}${card("Maximum height",z.max_height_m,true," m")}</div>
 ${present(z.remarks)?`<p class="muted"><b>Remarks:</b> ${esc(z.remarks)}</p>`:""}</div>`).join("");
}

function renderUnitTable(rows){
 if(!rows?.length)return '<div class="muted">No related registered units returned.</div>';
 return `<div class="table-wrap"><table><thead><tr><th>Registration</th><th>Type</th><th>Plan</th><th>Floor</th><th>2021</th><th>2018</th><th>1980</th><th>Enclosed</th><th>Covered</th><th>Uncovered</th></tr></thead><tbody>
 ${rows.map(x=>`<tr><td>${esc(present(x.registration_block)&&present(x.registration_no)?x.registration_block+"/"+x.registration_no:x.registration_no)}</td><td>${esc(x.kind||x.property_type)}</td><td>${esc(x.plan_no)}</td><td>${esc(x.unit_floor_no)}</td><td>${esc(money(x.price_2021))}</td><td>${esc(money(x.price_2018))}</td><td>${esc(money(x.price_1980))}</td><td>${esc(x.enclosed_extent)}</td><td>${esc(x.covered_extent)}</td><td>${esc(x.uncovered_extent)}</td></tr>`).join("")}
 </tbody></table></div>`;
}

async function selectSite(lat,lon){
 ["overview","planning","potential","geometry","terrain","spatial","warnings","valuation","registrations","units"]
 .forEach(id=>$(id).innerHTML='<div class="muted">Loading parcel and parcel-wide DLS checks…</div>');

 const r=await fetch(`/api/site?lat=${lat}&lon=${lon}`);
 const d=await r.json();
 if(!r.ok){alert(d.detail||"Lookup failed");return}

 if(selected)map.removeLayer(selected);
 selected=L.geoJSON(d.parcel_feature,{style:{color:"#ff7a00",weight:4,fillColor:"#ffb15c",fillOpacity:.26}}).addTo(map);
 map.fitBounds(selected.getBounds(),{padding:[25,25],maxZoom:19});

 const p=d.parcel;
 $("overview").innerHTML=`<div class="notice">Official DLS parcel data combined with parcel-wide DLS map-layer checks.</div>
 <div class="summary-pills" style="margin-top:10px"><div class="pill">Parcel ${esc(p.parcel_number)}</div><div class="pill">${esc(p.parcel_extent_m2)} m²</div><div class="pill">${esc(p.district)}</div><div class="pill">${esc(p.quarter)}</div></div>
 <div class="grid" style="margin-top:10px">${card("Parcel number",p.parcel_number)}${card("Official parcel area",p.parcel_extent_m2,false," m²")}${card("Map geometry area",p.map_geometry_extent_m2,false," m²")}${card("District",p.district)}${card("Municipality / community",p.municipality)}${card("Quarter",p.quarter)}${card("Postal code",p.postal_code)}${card("Sheet",p.sheet)}${card("Plan",p.plan)}${card("Block",p.block)}${card("Scale",p.scale)}${card("Registration no.",p.registration_number)}</div>`;

 $("planning").innerHTML=renderZones(d.planning_zones);

 $("potential").innerHTML=`<div class="grid">${card("Theoretical max floor area",d.development_potential.theoretical_max_floor_area_m2,true," m²")}${card("Theoretical max ground coverage",d.development_potential.theoretical_max_ground_coverage_m2,true," m²")}</div>
 <p class="muted">Calculated from DLS parcel area and planning coefficients. These are theoretical planning indicators, not guaranteed development rights.</p>`;

 const g=d.geometry_metrics||{};
 $("geometry").innerHTML=`<div class="grid">${card("Approx. perimeter",g.approx_perimeter_m,false," m")}${card("Longest edge",g.longest_edge_m,false," m")}${card("Shortest edge",g.shortest_edge_m,false," m")}${card("Longest-edge orientation",present(g.longest_edge_orientation)?g.longest_edge_orientation+" · "+g.longest_edge_orientation_deg+"°":"—")}${card("Centroid latitude",g.centroid_lat)}${card("Centroid longitude",g.centroid_lon)}</div>
 <p class="muted">Geometry metrics are calculated by the platform from the DLS parcel polygon and are approximate.</p>`;

 const b=d.building_summary||{};
 const c=d.contour_summary||{};
 $("terrain").innerHTML=`<div class="grid">${card("DLS building features",b.count,true)}${card("Contour lines intersecting parcel",c.count,true)}${card("Minimum contour elevation",c.min_elevation_m,false," m")}${card("Maximum contour elevation",c.max_elevation_m,false," m")}${card("Approx. elevation range",c.elevation_range_m,false," m")}</div>
 ${b.features?.length?`<div class="summary-pills" style="margin-top:10px">${b.features.map(x=>`<div class="pill">${esc(x.building_description||"Building")} ${present(x.building_code)?"· code "+esc(x.building_code):""}</div>`).join("")}</div>`:""}
 ${c.values_m?.length?`<p class="muted"><b>Contour values:</b> ${c.values_m.map(esc).join(", ")} m</p>`:""}`;

 $("spatial").innerHTML=Object.entries(d.spatial_checks||{}).map(([id,x])=>{
   const count=x.ok?(x.features||[]).length:null;
   return `<div class="check"><b>${esc(x.layer_name)}</b><br><span class="muted">${x.ok?`${count} intersecting feature(s)`:`Check unavailable`}</span></div>`;
 }).join("");

 $("warnings").innerHTML=d.warnings?.length?d.warnings.map(w=>`<div class="warning">${esc(w)}</div>`).join(""):'<div class="muted">No automatic warnings generated.</div>';

 $("valuation").innerHTML=`<div class="grid">${card("General valuation 1.1.2021",money(p.price_2021),true)}${card("General valuation 1.1.2018",money(p.price_2018),true)}${card("General valuation 1.1.1980",money(p.price_1980))}${card("Change 2018 → 2021",present(p.valuation_change_percent)?(p.valuation_change_percent>0?"+":"")+p.valuation_change_percent+"%":"—")}</div>
 <p class="muted">DLS general valuation values are for taxation and fee purposes and are not market valuations.</p>`;

 const reg=d.registration_summary;
 $("registrations").innerHTML=`<div class="grid">${card("Total related registrations",reg.total_related_records,true)}${card("Total enclosed extent",reg.total_enclosed_extent_m2,false," m²")}${card("Total covered extent",reg.total_covered_extent_m2,false," m²")}${card("Total uncovered extent",reg.total_uncovered_extent_m2,false," m²")}</div>
 <div class="summary-pills" style="margin-top:10px">${Object.entries(reg.by_type||{}).map(([k,v])=>`<div class="pill">${esc(k)}: ${esc(v)}</div>`).join("")}</div>`;

 $("units").innerHTML=renderUnitTable(d.related_properties);
}

map.on("click",e=>{
 if(map.getZoom()<15){alert("Zoom in further before selecting a parcel.");return}
 selectSite(e.latlng.lat,e.latlng.lng);
});

$("searchForm").addEventListener("submit",async e=>{
 e.preventDefault();
 const q=$("searchInput").value.trim();
 if(!q)return;
 const r=await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
 const d=await r.json();
 $("results").innerHTML="";
 (d.results||[]).forEach(x=>{
   const b=document.createElement("button");
   b.className="result";b.type="button";b.textContent=x.display_name;
   b.onclick=()=>map.setView([x.lat,x.lon],18);
   $("results").appendChild(b);
 });
});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def homepage():
    return HTMLResponse(HTML)
