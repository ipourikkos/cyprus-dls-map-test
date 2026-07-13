
import json
from collections import Counter, defaultdict
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="Cyprus DLS Site Explorer V9", version="0.9.0")

DLS_MAPSERVER = "https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer"
PARCEL_QUERY = f"{DLS_MAPSERVER}/0/query"
GENERAL_IDENTIFY = "https://eservices.dls.moi.gov.cy/Services/Rest/Info/GeneralParcelIdentify"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

GEOCODE_CACHE: dict[str, list[dict[str, Any]]] = {}


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.9.0"}


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
        "User-Agent": "CyprusDLSSiteExplorer/0.9",
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
        "User-Agent": "Mozilla/5.0 CyprusDLSSiteExplorer/0.9",
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


def safe_sum(values):
    total = 0.0
    found = False
    for v in values:
        if v in (None, ""):
            continue
        try:
            total += float(v)
            found = True
        except Exception:
            continue
    return round(total, 2) if found else None


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
    total_enclosed = []
    total_covered = []
    total_uncovered = []

    for rec in records:
        if rec is parcel:
            continue

        subitems = rec.get("PrPropertySubproperty") or []
        sub = subitems[0] if subitems else {}

        kind = clean_text(rec.get("SubPropertyKindName"))
        prop_type = clean_text(rec.get("PropertyTypeName"))

        label = kind or prop_type or "Other"
        type_counter[label] += 1

        enclosed = sub.get("PrEnclosedExtent")
        covered = sub.get("PrCoveredExtent")
        uncovered = sub.get("PrUncoveredExtent")

        total_enclosed.append(enclosed)
        total_covered.append(covered)
        total_uncovered.append(uncovered)

        related.append(
            {
                "property_type": prop_type,
                "kind": kind,
                "flat_no": clean_text(rec.get("PrFlatNo")),
                "house_no": rec.get("PrHouseNo"),
                "common_share": rec.get("PrCommonShare"),
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
            }
        )

    parcel_area = parcel.get("PrParcelExtent")
    theoretical_max_floor_area = None
    theoretical_max_ground_coverage = None

    if parcel_area not in (None, "") and zones:
        try:
            area = float(parcel_area)

            if len(zones) == 1:
                z = zones[0]
                if z["density_percent"] is not None:
                    theoretical_max_floor_area = round(
                        area * float(z["density_percent"]) / 100, 2
                    )
                if z["coverage_percent"] is not None:
                    theoretical_max_ground_coverage = round(
                        area * float(z["coverage_percent"]) / 100, 2
                    )
            else:
                floor_sum = 0.0
                coverage_sum = 0.0
                floor_ok = False
                coverage_ok = False

                for z in zones:
                    overlap = z.get("overlap_percent")
                    if overlap is None:
                        continue

                    affected_area = area * float(overlap) / 100

                    if z.get("density_percent") is not None:
                        floor_sum += affected_area * float(z["density_percent"]) / 100
                        floor_ok = True

                    if z.get("coverage_percent") is not None:
                        coverage_sum += affected_area * float(z["coverage_percent"]) / 100
                        coverage_ok = True

                if floor_ok:
                    theoretical_max_floor_area = round(floor_sum, 2)
                if coverage_ok:
                    theoretical_max_ground_coverage = round(coverage_sum, 2)

        except Exception:
            pass

    value_2021 = parcel.get("PrPriceBase2")
    value_2018 = parcel.get("PrPriceBase1")
    valuation_change_percent = None

    try:
        if value_2021 is not None and value_2018 not in (None, 0):
            valuation_change_percent = round(
                (float(value_2021) - float(value_2018))
                / float(value_2018)
                * 100,
                2,
            )
    except Exception:
        pass

    warnings = []

    if len(zones) > 1:
        warnings.append("Parcel is affected by multiple planning zones.")

    if any(z.get("remarks") for z in zones):
        warnings.append("One or more planning-zone remarks apply.")

    if related:
        warnings.append(f"Parcel has {len(related)} related registered properties or units.")

    if bool(parcel.get("PrIsPreserved")):
        warnings.append("Property is marked as preserved.")

    if bool(parcel.get("PrIsAncient")):
        warnings.append("Property is marked as ancient.")

    if bool(parcel.get("PrIsCommonProperty")):
        warnings.append("Property is marked as common property.")

    summary = {
        "parcel_number": clean_text(parcel.get("PrParcelNo")),
        "registration_number": clean_text(parcel.get("PrRegistrationNo")),
        "registration_block": parcel.get("PrRegistrationBlock"),
        "registration_date": parcel.get("PrRegistrationDate"),
        "district": clean_text(parcel.get("PrDistrictNameEn") or parcel.get("DistrictName")),
        "district_gr": clean_text(parcel.get("PrDistrictNameEl")),
        "municipality": clean_text(parcel.get("PrMunicipalityNameEn") or parcel.get("MunicipalityName")),
        "municipality_gr": clean_text(parcel.get("PrMunicipalityNameEl")),
        "quarter": clean_text(parcel.get("PrQuarterNameEn") or parcel.get("QuarterName")),
        "quarter_gr": clean_text(parcel.get("PrQuarterNameEl")),
        "sheet": clean_text(parcel.get("PrSheetValue")),
        "plan": clean_text(parcel.get("PrPlanValue")),
        "block": clean_text(parcel.get("PrBlockValue")),
        "scale": clean_text(parcel.get("PrScaleValue")),
        "postal_code": clean_text(parcel.get("PrPostalCode")),
        "house_no": parcel.get("PrHouseNo"),
        "flat_no": clean_text(parcel.get("PrFlatNo")),
        "parcel_extent_m2": parcel_area,
        "property_type": clean_text(parcel.get("PropertyTypeName")),
        "subproperty_kind": clean_text(parcel.get("SubPropertyKindName")),
        "price_2021": value_2021,
        "price_2018": value_2018,
        "price_1980": parcel.get("PrPriceBase3"),
        "valuation_change_percent": valuation_change_percent,
        "price_2021_date": parcel.get("PrPriceBase2Date"),
        "is_preserved": bool(parcel.get("PrIsPreserved")),
        "is_ancient": bool(parcel.get("PrIsAncient")),
        "is_depressed": bool(parcel.get("PrIsDepressed")),
        "occupation_status": parcel.get("PrOccupationStatus"),
        "is_common_property": bool(parcel.get("PrIsCommonProperty")),
        "common_share": parcel.get("PrCommonShare"),
        "sewage_code": parcel.get("PrSewageCode"),
        "is_new_cadastral_plan": bool(parcel.get("PrIsNewCadastralPlan")),
        "is_not_registration_based": bool(parcel.get("PrIsNotRegBased")),
        "parcel_id": parcel.get("PrParcelId"),
        "property_id": parcel.get("PrPropertyId"),
        "dlo_file_id": parcel.get("PrDLOFileId"),
    }

    return {
        "parcel_feature": parcel_feature,
        "parcel": summary,
        "planning_zones": zones,
        "development_potential": {
            "theoretical_max_floor_area_m2": theoretical_max_floor_area,
            "theoretical_max_ground_coverage_m2": theoretical_max_ground_coverage,
        },
        "registration_summary": {
            "total_related_records": len(related),
            "by_type": dict(type_counter),
            "total_enclosed_extent_m2": safe_sum(total_enclosed),
            "total_covered_extent_m2": safe_sum(total_covered),
            "total_uncovered_extent_m2": safe_sum(total_uncovered),
        },
        "related_properties": related,
        "warnings": warnings,
        "advanced": {
            "map_layer_attributes": map_props,
            "main_parcel_record": parcel,
        },
    }


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyprus DLS Site Explorer V9</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{
  --ink:#17211b;
  --green:#173f2b;
  --muted:#68726c;
  --line:#dfe5e0;
  --bg:#f4f5f3;
  --card:#f7f8f7;
  --warn:#fff4dc;
}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}
header{
  min-height:72px;
  padding:14px 20px;
  background:#fff;
  border-bottom:1px solid var(--line);
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
}
h1{font-size:20px;margin:2px 0 0}
.eyebrow{font-size:10px;font-weight:800;letter-spacing:.14em;color:var(--muted)}
.header-actions{display:flex;gap:8px}
.layout{display:grid;grid-template-columns:520px 1fr;height:calc(100vh - 72px)}
aside{background:#fff;border-right:1px solid var(--line);padding:16px;overflow:auto}
#map{height:100%}
form{display:flex;gap:8px}
.search{flex:1;padding:12px;border:1px solid #ccd4ce;border-radius:11px;font:inherit}
button{
  border:0;
  border-radius:11px;
  background:var(--green);
  color:#fff;
  padding:11px 14px;
  font-weight:750;
  cursor:pointer
}
.secondary{background:#edf3ef;color:var(--green)}
.result{width:100%;display:block;margin-top:7px;text-align:left;background:#edf3ef;color:var(--ink)}
.section{margin-top:20px;padding-top:17px;border-top:1px solid var(--line)}
.section h2{font-size:16px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.card{background:var(--card);border:1px solid #e5e9e6;border-radius:12px;padding:11px}
.label{font-size:10px;letter-spacing:.07em;text-transform:uppercase;font-weight:800;color:var(--muted)}
.value{font-weight:780;margin-top:4px;word-break:break-word}
.big{font-size:26px;color:var(--green)}
.zone{
  border:1px solid var(--line);
  border-radius:15px;
  padding:14px;
  margin-bottom:12px
}
.zone-title{font-size:31px;font-weight:850;color:var(--green)}
.badge{
  display:inline-block;
  margin-top:8px;
  background:#e8f0ea;
  color:var(--green);
  font-size:11px;
  font-weight:800;
  padding:5px 8px;
  border-radius:999px
}
.muted{font-size:12px;color:var(--muted);line-height:1.5}
.notice{background:#e9f6ec;border:1px solid #bbdec3;border-radius:10px;padding:10px;font-size:12px;line-height:1.5}
.warning{background:var(--warn);border:1px solid #ead39d;border-radius:10px;padding:10px;font-size:12px;line-height:1.45;margin-top:7px}
.summary-pills{display:flex;gap:7px;flex-wrap:wrap}
.pill{background:#edf3ef;color:var(--green);padding:6px 9px;border-radius:999px;font-size:12px;font-weight:750}
details{margin-top:12px;border:1px solid var(--line);border-radius:12px;background:#fafbfa}
summary{cursor:pointer;padding:11px 12px;font-weight:750}
.raw{font-family:ui-monospace,Consolas,monospace;font-size:11px;white-space:pre-wrap;background:#f3f5f3;border-radius:8px;padding:8px;max-height:350px;overflow:auto}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:920px}
th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#f3f5f3;font-size:10px;letter-spacing:.05em;text-transform:uppercase}
@media(max-width:900px){
  .layout{grid-template-columns:1fr;height:auto}
  #map{height:65vh}
}
@media print{
  body{background:#fff}
  header{position:static}
  .layout{display:block;height:auto}
  aside{border:0;overflow:visible}
  #map, form, #results, .header-actions, details{display:none!important}
  .section{break-inside:avoid}
  .table-wrap{overflow:visible}
  table{min-width:0;font-size:9px}
}
</style>
</head>
<body>
<header>
  <div>
    <div class="eyebrow">DLS SITE INTELLIGENCE REPORT</div>
    <h1>Cyprus Site Explorer V9</h1>
  </div>
  <div class="header-actions">
    <button class="secondary" onclick="window.print()">Print / Save PDF</button>
  </div>
</header>

<div class="layout">
<aside>
  <form id="searchForm">
    <input id="searchInput" class="search" placeholder="Search address in Cyprus">
    <button>Search</button>
  </form>

  <div id="results"></div>

  <section class="section">
    <h2>Overview</h2>
    <div id="overview" class="muted">Search, zoom in and click a parcel.</div>
  </section>

  <section class="section">
    <h2>Planning</h2>
    <div id="planning" class="muted">No parcel selected.</div>
  </section>

  <section class="section">
    <h2>Development potential</h2>
    <div id="potential" class="muted">No parcel selected.</div>
  </section>

  <section class="section">
    <h2>Warnings</h2>
    <div id="warnings" class="muted">No parcel selected.</div>
  </section>

  <section class="section">
    <h2>Valuation</h2>
    <div id="valuation" class="muted">No parcel selected.</div>
  </section>

  <section class="section">
    <h2>Registrations on parcel</h2>
    <div id="registrations" class="muted">No parcel selected.</div>
  </section>

  <section class="section">
    <h2>All registered units</h2>
    <div id="units" class="muted">No parcel selected.</div>
  </section>

  <details>
    <summary>Advanced DLS data</summary>
    <div style="padding:0 12px 12px">
      <div id="advanced"></div>
    </div>
  </details>
</aside>

<div id="map"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/esri-leaflet@3.0.15/dist/esri-leaflet.js"></script>
<script>
const DLS="https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer";

const map=L.map("map").setView([35.1264,33.4299],9);

L.tileLayer(
  "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom:20,attribution:"&copy; OpenStreetMap contributors"}
).addTo(map);

L.esri.dynamicMapLayer({
  url:DLS,
  layers:[0],
  opacity:1,
  minZoom:15
}).addTo(map);

let selected=null;

const $=id=>document.getElementById(id);

const esc=v=>String(v??"—")
  .replaceAll("&","&amp;")
  .replaceAll("<","&lt;")
  .replaceAll(">","&gt;");

const present=v=>!(v===null||v===undefined||v==="");

function card(label,value,big=false,suffix=""){
  return `<div class="card">
    <div class="label">${esc(label)}</div>
    <div class="value ${big?"big":""}">${present(value)?esc(value)+suffix:"—"}</div>
  </div>`;
}

function money(v){
  if(!present(v)) return "—";
  return "€"+Number(v).toLocaleString(undefined,{maximumFractionDigits:2});
}

function renderZones(zones){
  if(!zones?.length){
    return `<div class="muted">No planning-zone data returned by DLS Identify.</div>`;
  }

  return zones.map(z=>`
    <div class="zone">
      <div class="label">Planning zone</div>
      <div class="zone-title">${esc(z.zone)}</div>

      ${present(z.overlap_percent)
        ? `<div class="badge">${esc(z.overlap_percent)}% of parcel</div>`
        : ""}

      <div class="grid" style="margin-top:12px">
        ${card("Building density / Δόμηση",z.density_percent,true,"%")}
        ${card("Coverage / Κάλυψη",z.coverage_percent,true,"%")}
        ${card("Maximum floors",z.max_floors,true)}
        ${card("Maximum height",z.max_height_m,true," m")}
      </div>

      ${present(z.description_gr)
        ? `<p class="muted">${esc(z.description_gr)}</p>`
        : ""}

      ${present(z.remarks)
        ? `<p class="muted"><b>Remarks:</b> ${esc(z.remarks)}</p>`
        : ""}
    </div>
  `).join("");
}

function renderUnitTable(rows){
  if(!rows?.length){
    return '<div class="muted">No related registered units returned.</div>';
  }

  return `<div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Registration</th>
          <th>Type</th>
          <th>Plan no.</th>
          <th>Floor</th>
          <th>Value 2021</th>
          <th>Value 2018</th>
          <th>Value 1980</th>
          <th>Enclosed</th>
          <th>Covered</th>
          <th>Uncovered</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(x=>`
          <tr>
            <td>${esc(
              present(x.registration_block) && present(x.registration_no)
                ? x.registration_block + "/" + x.registration_no
                : x.registration_no
            )}</td>
            <td>${esc(x.kind || x.property_type)}</td>
            <td>${esc(x.plan_no)}</td>
            <td>${esc(x.unit_floor_no)}</td>
            <td>${esc(money(x.price_2021))}</td>
            <td>${esc(money(x.price_2018))}</td>
            <td>${esc(money(x.price_1980))}</td>
            <td>${esc(x.enclosed_extent)}</td>
            <td>${esc(x.covered_extent)}</td>
            <td>${esc(x.uncovered_extent)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  </div>`;
}

async function selectSite(lat,lon){
  [
    "overview","planning","potential","warnings",
    "valuation","registrations","units","advanced"
  ].forEach(
    id=>$(id).innerHTML='<div class="muted">Loading full DLS Identify data…</div>'
  );

  const r=await fetch(`/api/site?lat=${lat}&lon=${lon}`);
  const d=await r.json();

  if(!r.ok){
    alert(d.detail||"Lookup failed");
    return;
  }

  if(selected) map.removeLayer(selected);

  selected=L.geoJSON(
    d.parcel_feature,
    {
      style:{
        color:"#ff7a00",
        weight:4,
        fillColor:"#ffb15c",
        fillOpacity:.26
      }
    }
  ).addTo(map);

  map.fitBounds(selected.getBounds(),{padding:[25,25],maxZoom:19});

  const p=d.parcel;

  $("overview").innerHTML=`
    <div class="notice">Official parcel data loaded from DLS GeneralParcelIdentify.</div>

    <div class="summary-pills" style="margin-top:10px">
      <div class="pill">Parcel ${esc(p.parcel_number)}</div>
      <div class="pill">${esc(p.parcel_extent_m2)} m²</div>
      <div class="pill">${esc(p.district)}</div>
      <div class="pill">${esc(p.quarter)}</div>
    </div>

    <div class="grid" style="margin-top:10px">
      ${card("Parcel number",p.parcel_number)}
      ${card("Area",p.parcel_extent_m2,false," m²")}
      ${card("District",p.district)}
      ${card("Municipality / community",p.municipality)}
      ${card("Quarter",p.quarter)}
      ${card("Postal code",p.postal_code)}
      ${card("Sheet",p.sheet)}
      ${card("Plan",p.plan)}
      ${card("Block",p.block)}
      ${card("Scale",p.scale)}
      ${card("Registration no.",p.registration_number)}
      ${card("House no.",p.house_no)}
    </div>
  `;

  $("planning").innerHTML=renderZones(d.planning_zones);

  $("potential").innerHTML=`
    <div class="grid">
      ${card(
        "Theoretical max floor area",
        d.development_potential.theoretical_max_floor_area_m2,
        true,
        " m²"
      )}
      ${card(
        "Theoretical max ground coverage",
        d.development_potential.theoretical_max_ground_coverage_m2,
        true,
        " m²"
      )}
    </div>

    <p class="muted">
      Calculated from DLS parcel area and planning coefficients.
      These are theoretical planning indicators, not guaranteed development rights.
    </p>
  `;

  $("warnings").innerHTML=d.warnings?.length
    ? d.warnings.map(w=>`<div class="warning">${esc(w)}</div>`).join("")
    : '<div class="muted">No automatic warnings generated from the returned DLS data.</div>';

  $("valuation").innerHTML=`
    <div class="grid">
      ${card("General valuation 2021",money(p.price_2021),true)}
      ${card("General valuation 2018",money(p.price_2018),true)}
      ${card("General valuation 1980",money(p.price_1980))}
      ${card(
        "Change 2018 → 2021",
        present(p.valuation_change_percent)
          ? (p.valuation_change_percent>0?"+":"")+p.valuation_change_percent+"%"
          : "—"
      )}
    </div>

    <p class="muted">
      DLS general valuation values are for taxation and fee purposes and are not market valuations.
    </p>
  `;

  const reg=d.registration_summary;

  $("registrations").innerHTML=`
    <div class="grid">
      ${card("Total related registrations",reg.total_related_records,true)}
      ${card("Total enclosed extent",reg.total_enclosed_extent_m2,false," m²")}
      ${card("Total covered extent",reg.total_covered_extent_m2,false," m²")}
      ${card("Total uncovered extent",reg.total_uncovered_extent_m2,false," m²")}
    </div>

    <div class="summary-pills" style="margin-top:10px">
      ${Object.entries(reg.by_type||{}).map(
        ([k,v])=>`<div class="pill">${esc(k)}: ${esc(v)}</div>`
      ).join("")}
    </div>
  `;

  $("units").innerHTML=renderUnitTable(d.related_properties);

  $("advanced").innerHTML=`
    <div class="raw">${esc(JSON.stringify(d.advanced,null,2))}</div>
  `;
}

map.on("click",e=>{
  if(map.getZoom()<15){
    alert("Zoom in further before selecting a parcel.");
    return;
  }
  selectSite(e.latlng.lat,e.latlng.lng);
});

$("searchForm").addEventListener("submit",async e=>{
  e.preventDefault();

  const q=$("searchInput").value.trim();
  if(!q) return;

  const r=await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  const d=await r.json();

  $("results").innerHTML="";

  (d.results||[]).forEach(x=>{
    const b=document.createElement("button");
    b.className="result";
    b.type="button";
    b.textContent=x.display_name;
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
