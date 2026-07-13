
import json
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="Cyprus DLS Site Explorer V7", version="0.7.0")

DLS_MAPSERVER = (
    "https://eservices.dls.moi.gov.cy/arcgis/rest/services/"
    "National/CadastralMap_EN/MapServer"
)
PARCEL_QUERY = f"{DLS_MAPSERVER}/0/query"
GENERAL_IDENTIFY = (
    "https://eservices.dls.moi.gov.cy/Services/Rest/Info/"
    "GeneralParcelIdentify"
)
NOMINATIM = "https://nominatim.openstreetmap.org/search"

GEOCODE_CACHE: dict[str, list[dict[str, Any]]] = {}


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.7.0"}


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
        "User-Agent": "CyprusDLSSiteExplorer/0.7",
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
        "geometry": json.dumps(
            {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}
        ),
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
    params = {"subPropertyId": subproperty_id}

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://eservices.dls.moi.gov.cy/",
        "User-Agent": "Mozilla/5.0 CyprusDLSSiteExplorer/0.7",
    }

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        r = await client.get(GENERAL_IDENTIFY, params=params, headers=headers)

    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"DLS GeneralParcelIdentify failed with status {r.status_code}.",
        )

    try:
        return r.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="DLS GeneralParcelIdentify returned an invalid response.",
        )


def pick_parcel_record(records, parcel_id):
    # Best match: actual parcel/land record for this parcel id.
    for item in records:
        if (
            item.get("PrParcelId") == parcel_id
            and item.get("PropertyTypeName") == "Parcel"
        ):
            return item

    for item in records:
        if item.get("PropertyTypeName") == "Parcel":
            return item

    return records[0] if records else None


def as_percent(value):
    if value is None:
        return None
    try:
        x = float(value)
    except Exception:
        return value
    return round(x * 100, 2) if abs(x) <= 5 else round(x, 2)


def clean_text(value):
    if isinstance(value, str):
        return value.strip()
    return value


def parse_zone(zone_obj, plan_zone=None):
    if not zone_obj:
        return None

    affected = None
    total = None
    overlap_pct = None

    if plan_zone:
        affected = plan_zone.get("PrAffectedExtent")
        total = plan_zone.get("PrTotalExtent")
        try:
            if affected is not None and total not in (None, 0):
                overlap_pct = round((float(affected) / float(total)) * 100, 2)
        except Exception:
            overlap_pct = None

    return {
        "zone": zone_obj.get("PrName"),
        "density_percent": as_percent(zone_obj.get("PrDensityRateQty")),
        "coverage_percent": as_percent(zone_obj.get("PrCoverageRate")),
        "max_floors": zone_obj.get("PrStoreyNoQty"),
        "max_height_m": zone_obj.get("PrHeightMSR"),
        "remarks": clean_text(zone_obj.get("PrRemarkDesc")),
        "name_en": clean_text(zone_obj.get("PrNameEn")),
        "name_gr": clean_text(zone_obj.get("PrNameGr")),
        "affected_extent": affected,
        "total_extent": total,
        "overlap_percent": overlap_pct,
        "raw": zone_obj,
    }


@app.get("/api/site")
async def site(
    lat: float = Query(ge=34.0, le=36.0),
    lon: float = Query(ge=31.0, le=35.0),
):
    parcel_feature = await get_parcel_at_point(lat, lon)
    props = parcel_feature.get("properties", {})

    # In the public parcel layer, SBPI_ID_NO is the internal parcel/subproperty id
    # used by the official DLS GeneralParcelIdentify request.
    subproperty_id = props.get("SBPI_ID_NO")

    if subproperty_id is None:
        raise HTTPException(
            status_code=502,
            detail="The DLS parcel did not return SBPI_ID_NO, so Identify data cannot be loaded.",
        )

    try:
        subproperty_id = int(subproperty_id)
    except Exception:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected DLS SBPI_ID_NO value: {subproperty_id}",
        )

    identify_records = await get_general_identify(subproperty_id)

    if not isinstance(identify_records, list) or not identify_records:
        raise HTTPException(
            status_code=502,
            detail="DLS GeneralParcelIdentify returned no parcel records.",
        )

    parcel_record = pick_parcel_record(identify_records, subproperty_id)

    if not parcel_record:
        raise HTTPException(
            status_code=502,
            detail="Could not find the main parcel record in the DLS Identify response.",
        )

    zones = []

    parcel_plan_zones = parcel_record.get("ParcelPlanZones") or []
    for item in parcel_plan_zones:
        zone_obj = item.get("PrPlanningZone")
        parsed = parse_zone(zone_obj, item)
        if parsed:
            zones.append(parsed)

    if not zones:
        parsed = parse_zone(parcel_record.get("PrPlanningZone"))
        if parsed:
            zones.append(parsed)

    buildings_units = []
    for record in identify_records:
        if record is parcel_record:
            continue

        buildings_units.append(
            {
                "property_type": clean_text(record.get("PropertyTypeName")),
                "subproperty_kind": clean_text(record.get("SubPropertyKindName")),
                "flat_no": clean_text(record.get("PrFlatNo")),
                "house_no": record.get("PrHouseNo"),
                "common_share": record.get("PrCommonShare"),
                "parcel_id": record.get("PrParcelId"),
                "property_id": record.get("PrPropertyId"),
                "subproperty_id": next(
                    (
                        x.get("PrSubPropertyId")
                        for x in (record.get("PrPropertySubproperty") or [])
                        if x.get("PrSubPropertyId") is not None
                    ),
                    None,
                ),
            }
        )

    parcel_summary = {
        "parcel_number": parcel_record.get("PrParcelNo"),
        "registration_number": parcel_record.get("PrRegistrationNo"),
        "district": clean_text(
            parcel_record.get("PrDistrictNameEn")
            or parcel_record.get("DistrictName")
        ),
        "district_gr": clean_text(parcel_record.get("PrDistrictNameEl")),
        "municipality": clean_text(
            parcel_record.get("PrMunicipalityNameEn")
            or parcel_record.get("MunicipalityName")
        ),
        "municipality_gr": clean_text(parcel_record.get("PrMunicipalityNameEl")),
        "quarter": clean_text(
            parcel_record.get("PrQuarterNameEn")
            or parcel_record.get("QuarterName")
        ),
        "quarter_gr": clean_text(parcel_record.get("PrQuarterNameEl")),
        "sheet": clean_text(parcel_record.get("PrSheetValue")),
        "plan": clean_text(parcel_record.get("PrPlanValue")),
        "block": clean_text(parcel_record.get("PrBlockValue")),
        "scale": clean_text(parcel_record.get("PrScaleValue")),
        "postal_code": clean_text(parcel_record.get("PrPostalCode")),
        "parcel_extent_m2": parcel_record.get("PrParcelExtent"),
        "property_type": clean_text(parcel_record.get("PropertyTypeName")),
        "subproperty_kind": clean_text(parcel_record.get("SubPropertyKindName")),
        "house_no": parcel_record.get("PrHouseNo"),
        "price_base_1": parcel_record.get("PrPriceBase1"),
        "price_base_2": parcel_record.get("PrPriceBase2"),
        "price_base_3": parcel_record.get("PrPriceBase3"),
        "price_base_2_date": parcel_record.get("PrPriceBase2Date"),
        "is_preserved": bool(parcel_record.get("PrIsPreserved")),
        "is_ancient": bool(parcel_record.get("PrIsAncient")),
        "is_depressed": bool(parcel_record.get("PrIsDepressed")),
        "is_common_property": bool(parcel_record.get("PrIsCommonProperty")),
        "sewage_code": parcel_record.get("PrSewageCode"),
        "parcel_id": parcel_record.get("PrParcelId"),
        "property_id": parcel_record.get("PrPropertyId"),
        "dlo_file_id": parcel_record.get("PrDLOFileId"),
    }

    return {
        "parcel_feature": parcel_feature,
        "parcel": parcel_summary,
        "planning_zones": zones,
        "related_properties": buildings_units,
        "identify_record_count": len(identify_records),
        "advanced": {
            "map_layer_attributes": props,
            "main_parcel_record": parcel_record,
        },
    }


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyprus DLS Site Explorer V7</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--ink:#17211b;--green:#173f2b;--muted:#68726c;--line:#dfe5e0;--bg:#f4f5f3;--card:#f7f8f7}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}
header{height:70px;padding:14px 20px;background:#fff;border-bottom:1px solid var(--line)}
h1{font-size:20px;margin:2px 0 0}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.14em;color:var(--muted)}
.layout{display:grid;grid-template-columns:470px 1fr;height:calc(100vh - 70px)}
aside{background:#fff;border-right:1px solid var(--line);padding:16px;overflow:auto}#map{height:100%}
form{display:flex;gap:8px}.search{flex:1;padding:12px;border:1px solid #ccd4ce;border-radius:11px;font:inherit}
button{border:0;border-radius:11px;background:var(--green);color:#fff;padding:11px 14px;font-weight:750;cursor:pointer}
.result{width:100%;display:block;margin-top:7px;text-align:left;background:#edf3ef;color:var(--ink)}
.section{margin-top:20px;padding-top:17px;border-top:1px solid var(--line)}.section h2{font-size:16px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.card{background:var(--card);border:1px solid #e5e9e6;border-radius:12px;padding:11px}
.label{font-size:10px;letter-spacing:.07em;text-transform:uppercase;font-weight:800;color:var(--muted)}
.value{font-weight:780;margin-top:4px;word-break:break-word}.big{font-size:26px;color:var(--green)}
.zone{border:1px solid var(--line);border-radius:15px;padding:14px;margin-bottom:12px}
.zone-title{font-size:31px;font-weight:850;color:var(--green)}
.badge{display:inline-block;margin-top:8px;background:#e8f0ea;color:var(--green);font-size:11px;font-weight:800;padding:5px 8px;border-radius:999px}
.muted{font-size:12px;color:var(--muted);line-height:1.5}
.notice{background:#e9f6ec;border:1px solid #bbdec3;border-radius:10px;padding:10px;font-size:12px;line-height:1.5}
details{margin-top:12px;border:1px solid var(--line);border-radius:12px;background:#fafbfa}
summary{cursor:pointer;padding:11px 12px;font-weight:750}
.raw{font-family:ui-monospace,Consolas,monospace;font-size:11px;white-space:pre-wrap;background:#f3f5f3;border-radius:8px;padding:8px;max-height:300px;overflow:auto}
.related{padding:9px 0;border-bottom:1px solid var(--line)}.related:last-child{border-bottom:0}
@media(max-width:900px){.layout{grid-template-columns:1fr;height:auto}#map{height:65vh}}
</style>
</head>
<body>
<header><div class="eyebrow">DLS IDENTIFY DATA</div><h1>Cyprus Site Explorer V7</h1></header>
<div class="layout">
<aside>
<form id="searchForm"><input id="searchInput" class="search" placeholder="Search address in Cyprus"><button>Search</button></form>
<div id="results"></div>

<section class="section"><h2>Parcel</h2><div id="parcel" class="muted">Search, zoom in and click a parcel.</div></section>
<section class="section"><h2>Planning</h2><div id="planning" class="muted">No parcel selected.</div></section>
<section class="section"><h2>Valuation & status</h2><div id="valuation" class="muted">No parcel selected.</div></section>
<section class="section"><h2>Buildings & units</h2><div id="related" class="muted">No parcel selected.</div></section>

<details>
<summary>Advanced DLS data</summary>
<div style="padding:0 12px 12px"><div id="advanced"></div></div>
</details>
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

function renderZones(zones){
 if(!zones?.length)return `<div class="muted">No planning-zone data returned by DLS Identify.</div>`;
 return zones.map(z=>`
  <div class="zone">
   <div class="label">Planning zone</div>
   <div class="zone-title">${esc(z.zone)}</div>
   ${present(z.overlap_percent)?`<div class="badge">${esc(z.overlap_percent)}% of parcel</div>`:""}
   <div class="grid" style="margin-top:12px">
    ${card("Building density / Δόμηση",z.density_percent,true,"%")}
    ${card("Coverage / Κάλυψη",z.coverage_percent,true,"%")}
    ${card("Maximum floors",z.max_floors,true)}
    ${card("Maximum height",z.max_height_m,true," m")}
   </div>
   ${present(z.name_gr)?`<p class="muted">${esc(z.name_gr)}</p>`:""}
   ${present(z.remarks)?`<p class="muted"><b>Remarks:</b> ${esc(z.remarks)}</p>`:""}
  </div>
 `).join("");
}

async function selectSite(lat,lon){
 ["parcel","planning","valuation","related","advanced"].forEach(id=>$(id).innerHTML='<div class="muted">Loading official DLS Identify data…</div>');

 const r=await fetch(`/api/site?lat=${lat}&lon=${lon}`);
 const d=await r.json();

 if(!r.ok){
  alert(d.detail||"Lookup failed");
  return;
 }

 if(selected)map.removeLayer(selected);
 selected=L.geoJSON(d.parcel_feature,{style:{color:"#ff7a00",weight:4,fillColor:"#ffb15c",fillOpacity:.26}}).addTo(map);
 map.fitBounds(selected.getBounds(),{padding:[25,25],maxZoom:19});

 const p=d.parcel;

 $("parcel").innerHTML=`
  <div class="notice">Loaded from the same DLS GeneralParcelIdentify endpoint used by the official Identify workflow.</div>
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
  </div>`;

 $("planning").innerHTML=renderZones(d.planning_zones);

 $("valuation").innerHTML=`<div class="grid">
  ${card("Base value 1",p.price_base_1)}
  ${card("Base value 2",p.price_base_2)}
  ${card("Base value 3",p.price_base_3)}
  ${card("Base value 2 date",p.price_base_2_date)}
  ${card("Preserved",p.is_preserved?"Yes":"No")}
  ${card("Ancient",p.is_ancient?"Yes":"No")}
  ${card("Depressed",p.is_depressed?"Yes":"No")}
  ${card("Common property",p.is_common_property?"Yes":"No")}
  ${card("Sewage code",p.sewage_code)}
 </div>`;

 $("related").innerHTML=d.related_properties?.length
  ? d.related_properties.map(x=>`
    <div class="related">
      <b>${esc(x.property_type||"Related property")}</b>
      ${present(x.subproperty_kind)?` · ${esc(x.subproperty_kind)}`:""}
      ${present(x.flat_no)?` · Unit ${esc(x.flat_no)}`:""}
    </div>`).join("")
  : '<div class="muted">No related building/unit records returned.</div>';

 $("advanced").innerHTML=`<div class="raw">${esc(JSON.stringify(d.advanced,null,2))}</div>`;
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
