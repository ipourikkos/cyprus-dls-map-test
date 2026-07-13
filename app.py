
import json
import math
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from shapely.geometry import shape

app = FastAPI(title="Cyprus DLS Site Explorer V5", version="0.5.0")

DLS = "https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer"
INSPIRE_LAND_USE = "https://eservices.dls.moi.gov.cy/inspire/rest/services/INSPIRE/LU_LandUse/MapServer"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

POINT_LAYERS = {
    "development_plan": 11,
    "postal_code": 13,
    "district": 15,
    "municipality_community": 16,
    "quarter": 17,
    "block": 18,
    "locality": 19,
    "coast_protection_zone": 31,
    "state_land": 32,
    "white_zone": 37,
    "municipality_cluster": 50,
}

GEOCODE_CACHE: dict[str, list[dict[str, Any]]] = {}


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.5.0"}


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
        "User-Agent": "CyprusDLSSiteExplorer/0.5",
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


async def arcgis_query(
    base: str,
    layer_id: int,
    *,
    geometry: dict[str, Any],
    geometry_type: str,
    out_fields: str = "*",
    return_geometry: bool = False,
    result_record_count: int = 1000,
):
    params = {
        "f": "geojson",
        "where": "1=1",
        "geometry": json.dumps(geometry),
        "geometryType": geometry_type,
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "resultRecordCount": result_record_count,
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.get(f"{base}/{layer_id}/query", params=params)

    if r.status_code != 200:
        return {"features": [], "_error": f"HTTP {r.status_code}"}

    try:
        data = r.json()
    except Exception:
        return {"features": [], "_error": "Invalid JSON response"}

    if "error" in data:
        return {"features": [], "_error": str(data["error"])}

    return data


def clean_props(props):
    result = {}
    for k, v in props.items():
        if isinstance(v, float) and math.isfinite(v):
            result[k] = round(v, 6)
        else:
            result[k] = v
    return result


def percent_value(value):
    if value is None or value == "":
        return None
    try:
        x = float(value)
    except Exception:
        return value
    return round(x * 100, 2) if abs(x) <= 5 else round(x, 2)


def first_nonempty(props, keys):
    for key in keys:
        value = props.get(key)
        if value not in (None, ""):
            return value
    return None


def zone_summary(props, overlap_pct=None):
    return {
        "zone": first_nonempty(props, ["PLNZNT_NAME", "PLNZNT_CODE"]),
        "zone_code": props.get("PLNZNT_CODE"),
        "density_percent": percent_value(props.get("PLNZNT_DENSITY_RATE_QTY")),
        "coverage_percent": percent_value(props.get("PLNZNT_COVERAGE_RATE_QTY")),
        "max_floors": props.get("PLNZNT_STOREY_NO_QTY"),
        "max_height_m": props.get("PLNZNT_HEIGHT_MSR"),
        "development_plan": first_nonempty(
            props,
            ["DEVP_DESC", "DEVT_DESC", "DEVP_CCD", "DEVP_CODE"]
        ),
        "zone_description": props.get("PLNZNT_DESC"),
        "zone_category": props.get("PLNZNCAT_DESC"),
        "remarks": props.get("PLNZNT_REMARK_DESC"),
        "overlap_percent_of_parcel": overlap_pct,
        "raw": clean_props(props),
    }


def geojson_to_esri_polygon(geom):
    if geom.get("type") == "Polygon":
        return {
            "rings": geom.get("coordinates", []),
            "spatialReference": {"wkid": 4326},
        }
    if geom.get("type") == "MultiPolygon":
        rings = []
        for poly in geom.get("coordinates", []):
            rings.extend(poly)
        return {"rings": rings, "spatialReference": {"wkid": 4326}}
    return None


@app.get("/api/site")
async def site(
    lat: float = Query(ge=34.0, le=36.0),
    lon: float = Query(ge=31.0, le=35.0),
):
    point = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}

    parcel_data = await arcgis_query(
        DLS,
        0,
        geometry=point,
        geometry_type="esriGeometryPoint",
        out_fields="*",
        return_geometry=True,
        result_record_count=5,
    )

    parcels = parcel_data.get("features", [])
    if not parcels:
        raise HTTPException(status_code=404, detail="No DLS parcel found at that point.")

    parcel_feature = parcels[0]
    parcel_props = clean_props(parcel_feature.get("properties", {}))
    parcel_geom = parcel_feature.get("geometry") or {}
    esri_polygon = geojson_to_esri_polygon(parcel_geom)

    zones = []
    if esri_polygon:
        zone_data = await arcgis_query(
            INSPIRE_LAND_USE,
            0,
            geometry=esri_polygon,
            geometry_type="esriGeometryPolygon",
            out_fields="*",
            return_geometry=True,
            result_record_count=1000,
        )

        try:
            parcel_shape = shape(parcel_geom)
            parcel_area_units = parcel_shape.area
        except Exception:
            parcel_shape = None
            parcel_area_units = 0

        for feature in zone_data.get("features", []):
            props = feature.get("properties", {})
            overlap = None

            if parcel_shape is not None and parcel_area_units > 0 and feature.get("geometry"):
                try:
                    zone_shape = shape(feature["geometry"])
                    intersection = parcel_shape.intersection(zone_shape)
                    overlap = round((intersection.area / parcel_area_units) * 100, 2)
                except Exception:
                    overlap = None

            zones.append(zone_summary(props, overlap))

        zones.sort(
            key=lambda z: (
                z["overlap_percent_of_parcel"] is not None,
                z["overlap_percent_of_parcel"] or 0,
            ),
            reverse=True,
        )

    context = {}
    for key, layer_id in POINT_LAYERS.items():
        data = await arcgis_query(
            DLS,
            layer_id,
            geometry=point,
            geometry_type="esriGeometryPoint",
            out_fields="*",
            return_geometry=False,
            result_record_count=50,
        )
        context[key] = {
            "features": [
                clean_props(f.get("properties", {}))
                for f in data.get("features", [])
            ]
        }

    building_count = None
    if esri_polygon:
        buildings = await arcgis_query(
            DLS,
            28,
            geometry=esri_polygon,
            geometry_type="esriGeometryPolygon",
            out_fields="OBJECTID",
            return_geometry=False,
            result_record_count=1000,
        )
        building_count = len(buildings.get("features", []))

    area = (
        parcel_props.get("SHAPE.STArea()")
        or parcel_props.get("Shape__Area")
        or parcel_props.get("SHAPE_STArea")
    )
    perimeter = (
        parcel_props.get("SHAPE.STLength()")
        or parcel_props.get("Shape__Length")
        or parcel_props.get("SHAPE_STLength")
    )

    return {
        "parcel_feature": parcel_feature,
        "parcel": parcel_props,
        "planning_zones": zones,
        "site_metrics": {
            "dls_geometry_area": area,
            "dls_geometry_perimeter": perimeter,
            "building_features_intersecting_parcel": building_count,
        },
        "context": context,
    }


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyprus DLS Site Explorer V5</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--ink:#152019;--green:#173f2b;--muted:#6c746f;--line:#dfe5e0;--bg:#f4f5f3;--card:#f7f8f7;--orange:#ff7a00}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}
header{height:70px;padding:14px 20px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}
h1{font-size:20px;margin:2px 0 0}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.14em;color:var(--muted)}
.layout{display:grid;grid-template-columns:440px 1fr;height:calc(100vh - 70px)}
aside{background:#fff;border-right:1px solid var(--line);padding:16px;overflow:auto}
#map{height:100%}
form{display:flex;gap:8px}.search{flex:1;padding:12px;border:1px solid #ccd4ce;border-radius:11px;font:inherit}
button{border:0;border-radius:11px;background:var(--green);color:#fff;padding:11px 14px;font-weight:750;cursor:pointer}
.result{width:100%;display:block;margin-top:7px;text-align:left;background:#edf3ef;color:var(--ink)}
.section{margin-top:20px;padding-top:17px;border-top:1px solid var(--line)}.section h2{font-size:16px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.card{background:var(--card);border:1px solid #e5e9e6;border-radius:12px;padding:11px}
.label{font-size:10px;letter-spacing:.07em;text-transform:uppercase;font-weight:800;color:var(--muted)}
.value{font-weight:780;margin-top:4px;word-break:break-word}
.big{font-size:26px;color:var(--green)}
.zone{border:1px solid var(--line);border-radius:15px;padding:14px;margin-bottom:12px}
.zone-title{font-size:31px;font-weight:850;color:var(--green);line-height:1}
.badge{display:inline-block;margin-top:8px;background:#e8f0ea;color:var(--green);font-size:11px;font-weight:800;padding:5px 8px;border-radius:999px}
.muted{font-size:12px;color:var(--muted);line-height:1.5}
.subtle{font-size:13px;color:var(--muted);margin-top:8px}
details{margin-top:12px;border:1px solid var(--line);border-radius:12px;background:#fafbfa}
summary{cursor:pointer;padding:11px 12px;font-weight:750}
.details-body{padding:0 12px 12px}
.raw{font-family:ui-monospace,Consolas,monospace;font-size:11px;white-space:pre-wrap;background:#f3f5f3;border-radius:8px;padding:8px;max-height:250px;overflow:auto}
.layer{padding:9px 0;border-bottom:1px solid var(--line)}.layer:last-child{border-bottom:0}
@media(max-width:900px){.layout{grid-template-columns:1fr;height:auto}#map{height:65vh}}
</style>
</head>
<body>
<header>
  <div><div class="eyebrow">DLS SITE INTELLIGENCE</div><h1>Cyprus Site Explorer</h1></div>
  <div class="muted">Clear planning summary first. Technical DLS data underneath.</div>
</header>

<div class="layout">
<aside>
  <form id="searchForm">
    <input id="searchInput" class="search" placeholder="Search address in Cyprus">
    <button>Search</button>
  </form>
  <div id="results"></div>

  <section class="section">
    <h2>Parcel</h2>
    <div id="parcel" class="muted">Search, zoom in and click a parcel.</div>
  </section>

  <section class="section">
    <h2>Planning</h2>
    <div id="planning" class="muted">No parcel selected.</div>
  </section>

  <section class="section">
    <h2>Site context</h2>
    <div id="context" class="muted">No parcel selected.</div>
  </section>

  <details>
    <summary>Advanced DLS data</summary>
    <div class="details-body">
      <div id="advanced" class="muted">No parcel selected.</div>
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

const osm=L.tileLayer(
  "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom:20,attribution:"&copy; OpenStreetMap contributors"}
).addTo(map);

const overlays={
  "DLS parcels":L.esri.dynamicMapLayer({url:DLS,layers:[0],opacity:1,minZoom:15}).addTo(map),
  "Planning zones":L.esri.dynamicMapLayer({url:DLS,layers:[12],opacity:.65,minZoom:12}),
  "Buildings":L.esri.dynamicMapLayer({url:DLS,layers:[28],opacity:.8,minZoom:15}),
  "Contours":L.esri.dynamicMapLayer({url:DLS,layers:[30],opacity:.7,minZoom:14}),
  "Coast Protection Zone":L.esri.dynamicMapLayer({url:DLS,layers:[31],opacity:.7}),
  "State Land":L.esri.dynamicMapLayer({url:DLS,layers:[32],opacity:.7}),
  "White Zones":L.esri.dynamicMapLayer({url:DLS,layers:[37],opacity:.7})
};

L.control.layers({"OpenStreetMap":osm},overlays,{collapsed:true}).addTo(map);

let selected=null;
const $=id=>document.getElementById(id);
const esc=v=>String(v??"—").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
const present=v=>!(v===null||v===undefined||v==="");

function card(label,value,big=false,suffix=""){
  return `<div class="card">
    <div class="label">${esc(label)}</div>
    <div class="value ${big?"big":""}">${present(value)?esc(value)+suffix:"—"}</div>
  </div>`;
}

function first(ctx,key){
  return ctx?.[key]?.features?.[0]||null;
}

function findReadableValue(obj, candidates){
  if(!obj)return null;
  for(const key of candidates){
    if(present(obj[key]))return obj[key];
  }
  return null;
}

function renderPlanning(zones){
  if(!zones?.length){
    return `<div class="muted">No detailed planning-zone record was returned for this parcel.</div>`;
  }

  return zones.map(z=>`
    <div class="zone">
      <div class="label">Planning zone</div>
      <div class="zone-title">${esc(z.zone)}</div>

      ${present(z.overlap_percent_of_parcel)
        ? `<div class="badge">${esc(z.overlap_percent_of_parcel)}% of parcel</div>`
        : ""}

      <div class="grid" style="margin-top:12px">
        ${card("Building density / Δόμηση",z.density_percent,true,"%")}
        ${card("Coverage / Κάλυψη",z.coverage_percent,true,"%")}
        ${card("Maximum floors",z.max_floors,true)}
        ${card("Maximum height",z.max_height_m,true," m")}
      </div>

      ${present(z.development_plan)
        ? `<div class="subtle"><b>Development plan:</b> ${esc(z.development_plan)}</div>`
        : ""}

      ${present(z.zone_category)
        ? `<div class="subtle"><b>Zone category:</b> ${esc(z.zone_category)}</div>`
        : ""}

      ${present(z.zone_description)
        ? `<div class="subtle">${esc(z.zone_description)}</div>`
        : ""}

      ${present(z.remarks)
        ? `<div class="subtle"><b>Remarks:</b> ${esc(z.remarks)}</div>`
        : ""}
    </div>
  `).join("");
}

async function selectSite(lat,lon){
  ["parcel","planning","context","advanced"].forEach(
    id=>$(id).innerHTML='<div class="muted">Loading official DLS data…</div>'
  );

  const r=await fetch(`/api/site?lat=${lat}&lon=${lon}`);
  const d=await r.json();

  if(!r.ok){
    alert(d.detail||"Lookup failed");
    return;
  }

  if(selected)map.removeLayer(selected);

  selected=L.geoJSON(d.parcel_feature,{
    style:{
      color:"#ff7a00",
      weight:4,
      fillColor:"#ffb15c",
      fillOpacity:.26
    }
  }).addTo(map);

  map.fitBounds(selected.getBounds(),{padding:[25,25],maxZoom:19});

  const p=d.parcel;

  $("parcel").innerHTML=`
    <div class="grid">
      ${card("Parcel number",p.PARCEL_NBR)}
      ${card("Sheet",p.SHEET)}
      ${card("Plan",p.PLAN_NBR)}
      ${card("District code",p.DIST_CODE)}
      ${card("Community code",p.VIL_CODE)}
      ${card("Block",p.BLCK_CODE)}
      ${card("Quarter",p.QRTR_CODE)}
      ${card("Buildings on parcel",d.site_metrics.building_features_intersecting_parcel)}
    </div>
  `;

  $("planning").innerHTML=renderPlanning(d.planning_zones);

  const district=first(d.context,"district");
  const community=first(d.context,"municipality_community");
  const locality=first(d.context,"locality");
  const postal=first(d.context,"postal_code");
  const coast=first(d.context,"coast_protection_zone");
  const stateLand=first(d.context,"state_land");
  const whiteZone=first(d.context,"white_zone");

  const districtName=findReadableValue(district,["DIST_NAME","NAME","DESCRIPTION","DESC"]);
  const communityName=findReadableValue(community,["VIL_NAME","MUNIC_NAME","COMM_NAME","NAME","DESCRIPTION","DESC"]);
  const localityName=findReadableValue(locality,["LOCAL_NAME","NAME","DESCRIPTION","DESC"]);
  const postalCode=findReadableValue(postal,["POST_CODE","POSTAL_CODE","ZIP_CODE","CODE","NAME"]);

  $("context").innerHTML=`
    <div class="grid">
      ${card("District",districtName)}
      ${card("Municipality / community",communityName)}
      ${card("Locality",localityName)}
      ${card("Postal code",postalCode)}
      ${card("Coast protection zone",coast?"Yes":"No")}
      ${card("State land",stateLand?"Yes":"No")}
      ${card("White zone",whiteZone?"Yes":"No")}
    </div>
  `;

  $("advanced").innerHTML=`
    <div class="layer">
      <div class="label">Raw parcel attributes</div>
      <div class="raw">${esc(JSON.stringify(d.parcel,null,2))}</div>
    </div>
    <div class="layer">
      <div class="label">Raw planning-zone attributes</div>
      <div class="raw">${esc(JSON.stringify(d.planning_zones.map(z=>z.raw),null,2))}</div>
    </div>
    <div class="layer">
      <div class="label">Raw DLS context</div>
      <div class="raw">${esc(JSON.stringify(d.context,null,2))}</div>
    </div>
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
  if(!q)return;

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
