import json
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="Cyprus DLS Parcel + Planning Test", version="0.2.0")

DLS_MAPSERVER = "https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer"
DLS_PARCEL_QUERY = f"{DLS_MAPSERVER}/0/query"
DLS_ZONE_QUERY = f"{DLS_MAPSERVER}/12/query"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
DLS_SCANNED_PLANS_PAGE = "https://portal.dls.moi.gov.cy/en/alles-ypiresies/saromena-ktimatika-schedia/"

GEOCODE_CACHE: dict[str, list[dict[str, Any]]] = {}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/geocode")
async def geocode(q: str = Query(min_length=3, max_length=200)) -> dict[str, Any]:
    query = q.strip()
    cache_key = query.casefold()
    if cache_key in GEOCODE_CACHE:
        return {"results": GEOCODE_CACHE[cache_key]}

    params = {
        "q": f"{query}, Cyprus",
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 5,
        "countrycodes": "cy",
    }
    headers = {
        "User-Agent": "CyprusDLSMapPrototype/0.2 (parcel-planning-test)",
        "Accept-Language": "en,el;q=0.8",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(NOMINATIM_SEARCH, params=params, headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Address search failed with status {response.status_code}.")

    raw_results = response.json()
    results = [
        {
            "display_name": item.get("display_name"),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "type": item.get("type"),
        }
        for item in raw_results
        if item.get("lat") and item.get("lon")
    ]
    GEOCODE_CACHE[cache_key] = results
    return {"results": results}


async def query_dls_feature(url: str, *, lat: float, lon: float, out_fields: str, record_count: int = 5) -> dict[str, Any]:
    params = {
        "f": "geojson",
        "where": "1=1",
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnGeometry": "true",
        "resultRecordCount": record_count,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, params=params)
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"DLS query failed with status {response.status_code}.")
    data = response.json()
    if "error" in data:
        raise HTTPException(status_code=502, detail=f"DLS error: {data['error']}")
    return data


@app.get("/api/site")
async def site(lat: float = Query(ge=34.0, le=36.0), lon: float = Query(ge=31.0, le=35.0)) -> dict[str, Any]:
    parcel_data = await query_dls_feature(
        DLS_PARCEL_QUERY,
        lat=lat,
        lon=lon,
        out_fields="OBJECTID,SBPI_ID_NO,DIST_CODE,VIL_CODE,QRTR_CODE,BLCK_CODE,PARCEL_NBR,SHEET,PLAN_NBR,SRC_SL_CODE",
    )
    parcel_features = parcel_data.get("features", [])
    if not parcel_features:
        raise HTTPException(status_code=404, detail="No DLS parcel was found at that point.")

    parcel_feature = parcel_features[0]
    pp = parcel_feature.get("properties", {})

    zone_data = await query_dls_feature(
        DLS_ZONE_QUERY,
        lat=lat,
        lon=lon,
        out_fields="OBJECTID,PLNZNT_NAME,PLNZNT_CODE,PLNZNT_DESC",
        record_count=10,
    )
    zones = []
    for feature in zone_data.get("features", []):
        props = feature.get("properties", {})
        zones.append({
            "name": props.get("PLNZNT_NAME"),
            "code": props.get("PLNZNT_CODE"),
            "description": props.get("PLNZNT_DESC"),
        })

    parcel = {
        "parcel_number": pp.get("PARCEL_NBR"),
        "sheet": pp.get("SHEET"),
        "plan": pp.get("PLAN_NBR"),
        "district_code": pp.get("DIST_CODE"),
        "community_code": pp.get("VIL_CODE"),
        "quarter_code": pp.get("QRTR_CODE"),
        "block_code": pp.get("BLCK_CODE"),
        "sbpi_id_no": pp.get("SBPI_ID_NO"),
        "object_id": pp.get("OBJECTID"),
        "source_scale_code": pp.get("SRC_SL_CODE"),
    }

    return {
        "parcel": parcel,
        "zones": zones,
        "parcel_feature": parcel_feature,
        "scanned_plans": {
            "official_page": DLS_SCANNED_PLANS_PAGE,
            "sheet": parcel.get("sheet"),
            "plan": parcel.get("plan"),
        },
    }


HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Cyprus Site Explorer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f3f4f2;color:#17211b}.app{min-height:100vh;display:grid;grid-template-rows:auto 1fr}header{padding:16px 22px;background:white;border-bottom:1px solid #dfe4e0;display:flex;align-items:center;justify-content:space-between;gap:16px;z-index:1000}.eyebrow{font-size:11px;letter-spacing:.14em;color:#68726c;font-weight:800}h1{margin:3px 0 0;font-size:22px}.status{color:#68726c;font-size:13px}.layout{min-height:0;display:grid;grid-template-columns:420px 1fr}.sidebar{background:#fff;border-right:1px solid #dfe4e0;padding:18px;overflow-y:auto;z-index:500}.search-box{display:flex;gap:8px}input{width:100%;border:1px solid #cfd8d1;border-radius:12px;padding:12px 13px;font:inherit;outline:none}button,.button-link{border:0;border-radius:12px;background:#173f2b;color:white;font:inherit;font-weight:700;padding:11px 15px;cursor:pointer;text-decoration:none;display:inline-block}.hint{color:#68726c;font-size:13px;line-height:1.5;margin:12px 0 18px}.results{display:grid;gap:8px;margin-bottom:20px}.result{background:#f5f7f5;color:#17211b;border:1px solid #dfe4e0;text-align:left;font-weight:500;line-height:1.4}.section{border-top:1px solid #dfe4e0;padding-top:18px;margin-top:18px}.section h2{font-size:17px;margin:0 0 12px}.empty{color:#68726c;line-height:1.55;font-size:14px}.details{display:grid;gap:10px}.detail{padding:11px 12px;background:#edf3ef;border-radius:11px}.detail-label{color:#68726c;font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:800;margin-bottom:3px}.detail-value{font-weight:700;word-break:break-word}.zone-card{padding:14px;border:1px solid #dfe4e0;border-radius:14px;margin-bottom:12px}.zone-name{font-size:24px;font-weight:800;color:#173f2b}.zone-desc{margin-top:7px;color:#68726c;line-height:1.45;font-size:13px}.warning{color:#7a5c00;background:#fff9df;border:1px solid #eadc9d;border-radius:10px;padding:10px 12px;font-size:12px;line-height:1.45;margin-top:10px}#map{width:100%;height:100%;min-height:calc(100vh - 78px);background:#dde4df}.map-note{position:absolute;left:50%;bottom:26px;transform:translateX(-50%);z-index:700;background:rgba(23,63,43,.92);color:white;padding:10px 14px;border-radius:999px;font-size:13px;pointer-events:none;box-shadow:0 8px 24px rgba(0,0,0,.18)}.error{color:#9b2c2c;background:#fff0f0;border:1px solid #e6c3c3;border-radius:10px;padding:10px 12px;font-size:13px;margin-top:10px}@media(max-width:900px){.layout{grid-template-columns:1fr;grid-template-rows:auto 68vh}.sidebar{border-right:0;border-bottom:1px solid #dfe4e0}#map{min-height:68vh}}
</style>
</head>
<body>
<div class="app">
<header><div><div class="eyebrow">DLS PARCEL + PLANNING TEST</div><h1>Cyprus Site Explorer</h1></div><div class="status">Official DLS parcel + planning-zone layers</div></header>
<div class="layout">
<aside class="sidebar">
<form id="searchForm" class="search-box"><input id="searchInput" placeholder="Search an address in Cyprus" autocomplete="off"/><button id="searchButton" type="submit">Search</button></form>
<p class="hint">Search for an address, zoom in, then click inside a DLS parcel.</p>
<div id="results" class="results"></div>
<section class="section"><h2>Parcel</h2><div id="parcelContent" class="empty">No parcel selected yet.</div></section>
<section class="section"><h2>Planning</h2><div id="zoneContent" class="empty">Select a parcel to load its planning zone.</div></section>
<section class="section"><h2>Cadastral plan PDF</h2><div id="planContent" class="empty">Select a parcel to see its sheet and plan details.</div></section>
<div id="errorBox"></div>
</aside>
<div style="position:relative;min-height:0"><div id="map"></div><div class="map-note">Zoom in and click inside a parcel</div></div>
</div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/esri-leaflet@3.0.15/dist/esri-leaflet.js"></script>
<script>
const DLS_MAPSERVER="https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer";
const map=L.map("map",{center:[35.1264,33.4299],zoom:9});
const osm=L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:20,attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
const dlsParcels=L.esri.dynamicMapLayer({url:DLS_MAPSERVER,layers:[0],opacity:1,minZoom:15}).addTo(map);
const dlsZones=L.esri.dynamicMapLayer({url:DLS_MAPSERVER,layers:[12],opacity:.65,minZoom:12});
L.control.layers({"OpenStreetMap":osm},{"DLS parcels":dlsParcels,"DLS planning zones":dlsZones},{collapsed:false}).addTo(map);
let selectedLayer=null,searchMarker=null;
const searchForm=document.getElementById("searchForm"),searchInput=document.getElementById("searchInput"),searchButton=document.getElementById("searchButton"),resultsBox=document.getElementById("results"),parcelContent=document.getElementById("parcelContent"),zoneContent=document.getElementById("zoneContent"),planContent=document.getElementById("planContent"),errorBox=document.getElementById("errorBox");
function clearError(){errorBox.innerHTML=""}function showError(m){errorBox.innerHTML=`<div class="error">${escapeHtml(m)}</div>`}function escapeHtml(v){return String(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")}function valueOrDash(v){return v===null||v===undefined||v===""?"—":String(v)}
function renderParcel(p){const rows=[["Parcel number",p.parcel_number],["Sheet",p.sheet],["Plan",p.plan],["District code",p.district_code],["Community code",p.community_code],["Quarter code",p.quarter_code],["Block code",p.block_code],["SBPI ID",p.sbpi_id_no]];parcelContent.className="details";parcelContent.innerHTML=rows.map(([l,v])=>`<div class="detail"><div class="detail-label">${escapeHtml(l)}</div><div class="detail-value">${escapeHtml(valueOrDash(v))}</div></div>`).join("")}
function renderZones(zones){if(!zones||!zones.length){zoneContent.className="empty";zoneContent.textContent="No planning zone was returned for the clicked point.";return}zoneContent.className="";zoneContent.innerHTML=zones.map(z=>`<div class="zone-card"><div class="zone-name">${escapeHtml(valueOrDash(z.name))}</div><div class="zone-desc">${escapeHtml(valueOrDash(z.description))}</div><div class="warning">The DLS planning-zone layer gives the zone name/code/description, but does not expose dedicated density, coverage, floor or height fields. We need to link those values to the authoritative planning-zone parameter source before showing them as definitive.</div></div>`).join("")}
function renderPlan(info){planContent.className="";planContent.innerHTML=`<div class="detail"><div class="detail-label">Selected sheet</div><div class="detail-value">${escapeHtml(valueOrDash(info.sheet))}</div></div><div class="detail" style="margin-top:8px"><div class="detail-label">Selected plan</div><div class="detail-value">${escapeHtml(valueOrDash(info.plan))}</div></div><p class="hint">The public parcel layer does not expose a direct scanned-PDF URL field. Open the official DLS scanned-plan page and use the selected sheet/plan details above.</p><a class="button-link" href="${escapeHtml(info.official_page)}" target="_blank" rel="noopener noreferrer">Open DLS scanned cadastral plans</a>`}
async function selectSite(lat,lon){clearError();parcelContent.className="empty";parcelContent.textContent="Finding DLS parcel…";zoneContent.className="empty";zoneContent.textContent="Loading planning zone…";planContent.className="empty";planContent.textContent="Loading cadastral plan details…";try{const r=await fetch(`/api/site?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`),data=await r.json();if(!r.ok)throw new Error(data.detail||"Site lookup failed.");if(selectedLayer)map.removeLayer(selectedLayer);selectedLayer=L.geoJSON(data.parcel_feature,{style:{color:"#ff7a00",weight:4,fillColor:"#ffb15c",fillOpacity:.28}}).addTo(map);map.fitBounds(selectedLayer.getBounds(),{padding:[30,30],maxZoom:19});renderParcel(data.parcel);renderZones(data.zones);renderPlan(data.scanned_plans)}catch(e){parcelContent.className="empty";parcelContent.textContent="No parcel selected.";zoneContent.className="empty";zoneContent.textContent="No planning information loaded.";planContent.className="empty";planContent.textContent="No cadastral plan loaded.";showError(e.message)}}
map.on("click",e=>{if(map.getZoom()<15){showError("Zoom in further before selecting a parcel.");return}selectSite(e.latlng.lat,e.latlng.lng)});
searchForm.addEventListener("submit",async e=>{e.preventDefault();const q=searchInput.value.trim();if(!q)return;clearError();resultsBox.innerHTML="";searchButton.disabled=true;searchButton.textContent="Searching…";try{const r=await fetch(`/api/geocode?q=${encodeURIComponent(q)}`),data=await r.json();if(!r.ok)throw new Error(data.detail||"Address search failed.");if(!data.results.length){resultsBox.innerHTML='<div class="empty">No Cyprus address results found.</div>';return}data.results.forEach(result=>{const b=document.createElement("button");b.className="result";b.type="button";b.textContent=result.display_name;b.addEventListener("click",()=>{map.setView([result.lat,result.lon],18);if(searchMarker)map.removeLayer(searchMarker);searchMarker=L.circleMarker([result.lat,result.lon],{radius:7,color:"#173f2b",weight:3,fillColor:"#fff",fillOpacity:1}).addTo(map)});resultsBox.appendChild(b)})}catch(e){showError(e.message)}finally{searchButton.disabled=false;searchButton.textContent="Search"}});
</script>
</body>
</html>'''


@app.get("/", response_class=HTMLResponse)
def homepage() -> HTMLResponse:
    return HTMLResponse(HTML)
