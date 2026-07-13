
import json
import math
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from shapely.geometry import shape

app = FastAPI(title="Cyprus DLS Site Explorer V6", version="0.6.0")

DLS = "https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer"
INSPIRE = "https://eservices.dls.moi.gov.cy/inspire/rest/services/INSPIRE/LU_LandUse/MapServer"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

GEOCODE_CACHE: dict[str, list[dict[str, Any]]] = {}


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.6.0"}


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
        "User-Agent": "CyprusDLSSiteExplorer/0.6",
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
        "geometry": json.dumps(geometry, separators=(",", ":")),
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
    out = {}
    for k, v in props.items():
        if isinstance(v, float) and math.isfinite(v):
            out[k] = round(v, 6)
        else:
            out[k] = v
    return out


def percent_value(value):
    if value is None or value == "":
        return None
    try:
        x = float(value)
    except Exception:
        return value
    return round(x * 100, 2) if abs(x) <= 5 else round(x, 2)


def zone_summary(props, overlap_pct=None):
    return {
        "zone": props.get("PLNZNT_NAME") or props.get("PLNZNT_CODE"),
        "density_percent": percent_value(props.get("PLNZNT_DENSITY_RATE_QTY")),
        "coverage_percent": percent_value(props.get("PLNZNT_COVERAGE_RATE_QTY")),
        "max_floors": props.get("PLNZNT_STOREY_NO_QTY"),
        "max_height_m": props.get("PLNZNT_HEIGHT_MSR"),
        "development_plan": props.get("DEVP_DESC") or props.get("DEVT_DESC"),
        "zone_description": props.get("PLNZNT_DESC"),
        "zone_category": props.get("PLNZNCAT_DESC"),
        "remarks": props.get("PLNZNT_REMARK_DESC"),
        "overlap_percent_of_parcel": overlap_pct,
        "raw": clean_props(props),
    }


def parcel_bbox(geom):
    if geom.get("type") == "Polygon":
        pts = [p for ring in geom.get("coordinates", []) for p in ring]
    elif geom.get("type") == "MultiPolygon":
        pts = [p for poly in geom.get("coordinates", []) for ring in poly for p in ring]
    else:
        return None
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {
        "xmin": min(xs),
        "ymin": min(ys),
        "xmax": max(xs),
        "ymax": max(ys),
        "spatialReference": {"wkid": 4326},
    }


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

    # IMPORTANT FIX:
    # First query the official INSPIRE planning layer by the exact clicked POINT.
    # This avoids the polygon-ring/orientation issue that caused V5 to return no records.
    point_zone_data = await arcgis_query(
        INSPIRE,
        0,
        geometry=point,
        geometry_type="esriGeometryPoint",
        out_fields="*",
        return_geometry=True,
        result_record_count=25,
    )

    zones = []
    seen = set()

    # Exact point result is the primary source.
    for feature in point_zone_data.get("features", []):
        props = feature.get("properties", {})
        key = (
            props.get("PLNZNT_NAME"),
            props.get("DEVP_CODE"),
            props.get("DLOF_ID_NO"),
        )
        if key not in seen:
            zones.append(zone_summary(props, 100.0))
            seen.add(key)

    # Then use a BOUNDING-BOX query to discover any other zones touching the parcel.
    # We filter those candidates with Shapely against the actual parcel polygon.
    bbox = parcel_bbox(parcel_geom)
    if bbox:
        candidate_data = await arcgis_query(
            INSPIRE,
            0,
            geometry=bbox,
            geometry_type="esriGeometryEnvelope",
            out_fields="*",
            return_geometry=True,
            result_record_count=1000,
        )

        try:
            parcel_shape = shape(parcel_geom)
            parcel_area = parcel_shape.area
        except Exception:
            parcel_shape = None
            parcel_area = 0

        if parcel_shape is not None and parcel_area > 0:
            for feature in candidate_data.get("features", []):
                props = feature.get("properties", {})
                geom = feature.get("geometry")
                if not geom:
                    continue
                try:
                    zshape = shape(geom)
                    inter = parcel_shape.intersection(zshape)
                    if inter.is_empty:
                        continue
                    overlap = round((inter.area / parcel_area) * 100, 2)
                    if overlap <= 0:
                        continue
                except Exception:
                    continue

                key = (
                    props.get("PLNZNT_NAME"),
                    props.get("DEVP_CODE"),
                    props.get("DLOF_ID_NO"),
                )

                if key in seen:
                    for existing in zones:
                        if (
                            existing["zone"] == (props.get("PLNZNT_NAME") or props.get("PLNZNT_CODE"))
                            and existing["raw"].get("DEVP_CODE") == props.get("DEVP_CODE")
                        ):
                            existing["overlap_percent_of_parcel"] = overlap
                            break
                else:
                    zones.append(zone_summary(props, overlap))
                    seen.add(key)

    zones.sort(
        key=lambda z: z.get("overlap_percent_of_parcel") or 0,
        reverse=True,
    )

    # Fallback to the ordinary DLS planning-zone layer for the zone name if INSPIRE fails.
    fallback_zone = None
    if not zones:
        fallback_data = await arcgis_query(
            DLS,
            12,
            geometry=point,
            geometry_type="esriGeometryPoint",
            out_fields="*",
            return_geometry=False,
            result_record_count=10,
        )
        feats = fallback_data.get("features", [])
        if feats:
            fallback_zone = clean_props(feats[0].get("properties", {}))

    context = {}
    for key, layer_id in {
        "district": 15,
        "municipality_community": 16,
        "quarter": 17,
        "block": 18,
        "locality": 19,
        "postal_code": 13,
        "coast_protection_zone": 31,
        "state_land": 32,
        "white_zone": 37,
    }.items():
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
            "features": [clean_props(f.get("properties", {})) for f in data.get("features", [])]
        }

    return {
        "parcel_feature": parcel_feature,
        "parcel": parcel_props,
        "planning_zones": zones,
        "fallback_zone": fallback_zone,
        "context": context,
        "diagnostics": {
            "inspire_point_error": point_zone_data.get("_error"),
            "inspire_point_feature_count": len(point_zone_data.get("features", [])),
        },
    }


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyprus DLS Site Explorer V6</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--ink:#17211b;--green:#173f2b;--muted:#68726c;--line:#dfe5e0;--bg:#f4f5f3;--card:#f7f8f7}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}
header{height:70px;padding:14px 20px;background:#fff;border-bottom:1px solid var(--line)}
h1{font-size:20px;margin:2px 0 0}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.14em;color:var(--muted)}
.layout{display:grid;grid-template-columns:440px 1fr;height:calc(100vh - 70px)}
aside{background:#fff;border-right:1px solid var(--line);padding:16px;overflow:auto}#map{height:100%}
form{display:flex;gap:8px}.search{flex:1;padding:12px;border:1px solid #ccd4ce;border-radius:11px;font:inherit}
button{border:0;border-radius:11px;background:var(--green);color:#fff;padding:11px 14px;font-weight:750;cursor:pointer}.result{width:100%;display:block;margin-top:7px;text-align:left;background:#edf3ef;color:var(--ink)}
.section{margin-top:20px;padding-top:17px;border-top:1px solid var(--line)}.section h2{font-size:16px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.card{background:var(--card);border:1px solid #e5e9e6;border-radius:12px;padding:11px}
.label{font-size:10px;letter-spacing:.07em;text-transform:uppercase;font-weight:800;color:var(--muted)}.value{font-weight:780;margin-top:4px}.big{font-size:26px;color:var(--green)}
.zone{border:1px solid var(--line);border-radius:15px;padding:14px;margin-bottom:12px}.zone-title{font-size:31px;font-weight:850;color:var(--green)}.badge{display:inline-block;margin-top:8px;background:#e8f0ea;color:var(--green);font-size:11px;font-weight:800;padding:5px 8px;border-radius:999px}
.muted{font-size:12px;color:var(--muted);line-height:1.5}.warn{background:#fff4dc;border:1px solid #ead39d;border-radius:10px;padding:10px;font-size:12px;line-height:1.5}
details{margin-top:12px;border:1px solid var(--line);border-radius:12px;background:#fafbfa}summary{cursor:pointer;padding:11px 12px;font-weight:750}.raw{font-family:ui-monospace,Consolas,monospace;font-size:11px;white-space:pre-wrap;background:#f3f5f3;border-radius:8px;padding:8px;max-height:260px;overflow:auto}
@media(max-width:900px){.layout{grid-template-columns:1fr;height:auto}#map{height:65vh}}
</style>
</head>
<body>
<header><div class="eyebrow">DLS SITE INTELLIGENCE</div><h1>Cyprus Site Explorer V6</h1></header>
<div class="layout">
<aside>
<form id="searchForm"><input id="searchInput" class="search" placeholder="Search address in Cyprus"><button>Search</button></form>
<div id="results"></div>
<section class="section"><h2>Parcel</h2><div id="parcel" class="muted">Search, zoom in and click a parcel.</div></section>
<section class="section"><h2>Planning</h2><div id="planning" class="muted">No parcel selected.</div></section>
<section class="section"><h2>Site context</h2><div id="context" class="muted">No parcel selected.</div></section>
<details><summary>Advanced diagnostics</summary><div style="padding:0 12px 12px"><div id="advanced"></div></div></details>
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

function card(label,value,big=false,suffix=""){
 return `<div class="card"><div class="label">${esc(label)}</div><div class="value ${big?"big":""}">${present(value)?esc(value)+suffix:"—"}</div></div>`;
}

function renderZones(zones,fallback){
 if(zones?.length){
  return zones.map(z=>`
   <div class="zone">
    <div class="label">Planning zone</div>
    <div class="zone-title">${esc(z.zone)}</div>
    ${present(z.overlap_percent_of_parcel)?`<div class="badge">${esc(z.overlap_percent_of_parcel)}% of parcel</div>`:""}
    <div class="grid" style="margin-top:12px">
      ${card("Building density / Δόμηση",z.density_percent,true,"%")}
      ${card("Coverage / Κάλυψη",z.coverage_percent,true,"%")}
      ${card("Maximum floors",z.max_floors,true)}
      ${card("Maximum height",z.max_height_m,true," m")}
    </div>
    ${present(z.development_plan)?`<p class="muted"><b>Development plan:</b> ${esc(z.development_plan)}</p>`:""}
    ${present(z.remarks)?`<p class="muted"><b>Remarks:</b> ${esc(z.remarks)}</p>`:""}
   </div>`).join("");
 }
 if(fallback){
  const name=fallback.PLNZNT_NAME??fallback.PLNZNT_CODE??"Unknown";
  return `<div class="warn"><b>Zone found: ${esc(name)}</b><br>The ordinary DLS zone layer returned the zone, but the detailed INSPIRE coefficient query did not return a matching record for this point.</div>`;
 }
 return `<div class="warn">No planning-zone record was returned from either official DLS planning service for this point.</div>`;
}

function first(ctx,key){return ctx?.[key]?.features?.[0]||null}

async function selectSite(lat,lon){
 ["parcel","planning","context","advanced"].forEach(id=>$(id).innerHTML='<div class="muted">Loading official DLS data…</div>');
 const r=await fetch(`/api/site?lat=${lat}&lon=${lon}`);
 const d=await r.json();
 if(!r.ok){alert(d.detail||"Lookup failed");return}

 if(selected)map.removeLayer(selected);
 selected=L.geoJSON(d.parcel_feature,{style:{color:"#ff7a00",weight:4,fillColor:"#ffb15c",fillOpacity:.26}}).addTo(map);
 map.fitBounds(selected.getBounds(),{padding:[25,25],maxZoom:19});

 const p=d.parcel;
 $("parcel").innerHTML=`<div class="grid">
  ${card("Parcel number",p.PARCEL_NBR)}
  ${card("Sheet",p.SHEET)}
  ${card("Plan",p.PLAN_NBR)}
  ${card("District code",p.DIST_CODE)}
  ${card("Community code",p.VIL_CODE)}
  ${card("Block",p.BLCK_CODE)}
 </div>`;

 $("planning").innerHTML=renderZones(d.planning_zones,d.fallback_zone);

 const district=first(d.context,"district");
 const community=first(d.context,"municipality_community");
 const postal=first(d.context,"postal_code");
 const coast=first(d.context,"coast_protection_zone");
 const stateLand=first(d.context,"state_land");

 $("context").innerHTML=`<div class="grid">
  ${card("District",district?JSON.stringify(district):"—")}
  ${card("Municipality / community",community?JSON.stringify(community):"—")}
  ${card("Postal code",postal?JSON.stringify(postal):"—")}
  ${card("Coast protection zone",coast?"Yes":"No")}
  ${card("State land",stateLand?"Yes":"No")}
 </div>`;

 $("advanced").innerHTML=`<div class="raw">${esc(JSON.stringify(d.diagnostics,null,2))}</div>`;
}

map.on("click",e=>{
 if(map.getZoom()<15){alert("Zoom in further before selecting a parcel.");return}
 selectSite(e.latlng.lat,e.latlng.lng);
});

$("searchForm").addEventListener("submit",async e=>{
 e.preventDefault();
 const q=$("searchInput").value.trim(); if(!q)return;
 const r=await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
 const d=await r.json();
 $("results").innerHTML="";
 (d.results||[]).forEach(x=>{
  const b=document.createElement("button");
  b.className="result"; b.type="button"; b.textContent=x.display_name;
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
