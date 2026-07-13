import json
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="Cyprus DLS Site Explorer", version="0.2.1")

DLS_MAPSERVER = (
    "https://eservices.dls.moi.gov.cy/arcgis/rest/services/"
    "National/CadastralMap_EN/MapServer"
)
PARCEL_QUERY = f"{DLS_MAPSERVER}/0/query"
ZONE_QUERY = f"{DLS_MAPSERVER}/12/query"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"

GEOCODE_CACHE: dict[str, list[dict[str, Any]]] = {}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/geocode")
async def geocode(q: str = Query(min_length=3, max_length=200)) -> dict[str, Any]:
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
        "User-Agent": "CyprusDLSSiteExplorer/0.2.1",
        "Accept-Language": "en,el;q=0.8",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(NOMINATIM_SEARCH, params=params, headers=headers)

    response.raise_for_status()
    results = [
        {
            "display_name": item.get("display_name"),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
        }
        for item in response.json()
        if item.get("lat") and item.get("lon")
    ]
    GEOCODE_CACHE[key] = results
    return {"results": results}


async def dls_point_query(url: str, lat: float, lon: float, out_fields: str) -> dict[str, Any]:
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
        "outFields": out_fields,
        "returnGeometry": "true",
        "resultRecordCount": 10,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, params=params)

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"DLS query failed: {response.status_code}")

    data = response.json()
    if "error" in data:
        raise HTTPException(status_code=502, detail=f"DLS error: {data['error']}")
    return data


@app.get("/api/site")
async def site(
    lat: float = Query(ge=34.0, le=36.0),
    lon: float = Query(ge=31.0, le=35.0),
) -> dict[str, Any]:
    parcel_data = await dls_point_query(
        PARCEL_QUERY,
        lat,
        lon,
        "OBJECTID,SBPI_ID_NO,DIST_CODE,VIL_CODE,QRTR_CODE,"
        "BLCK_CODE,PARCEL_NBR,SHEET,PLAN_NBR,SRC_SL_CODE",
    )

    features = parcel_data.get("features", [])
    if not features:
        raise HTTPException(status_code=404, detail="No DLS parcel found at that point.")

    parcel_feature = features[0]
    props = parcel_feature.get("properties", {})

    zone_data = await dls_point_query(
        ZONE_QUERY,
        lat,
        lon,
        "OBJECTID,PLNZNT_NAME,PLNZNT_CODE,PLNZNT_DESC",
    )

    zones = [
        {
            "name": z.get("properties", {}).get("PLNZNT_NAME"),
            "code": z.get("properties", {}).get("PLNZNT_CODE"),
            "description": z.get("properties", {}).get("PLNZNT_DESC"),
        }
        for z in zone_data.get("features", [])
    ]

    return {
        "parcel": {
            "parcel_number": props.get("PARCEL_NBR"),
            "sheet": props.get("SHEET"),
            "plan": props.get("PLAN_NBR"),
            "district_code": props.get("DIST_CODE"),
            "community_code": props.get("VIL_CODE"),
            "quarter_code": props.get("QRTR_CODE"),
            "block_code": props.get("BLCK_CODE"),
            "sbpi_id_no": props.get("SBPI_ID_NO"),
        },
        "zones": zones,
        "parcel_feature": parcel_feature,
    }


HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Cyprus DLS Site Explorer</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    body{margin:0;font-family:Arial,sans-serif;background:#f3f4f2;color:#17211b}
    header{padding:14px 18px;background:#fff;border-bottom:1px solid #ddd}
    .layout{display:grid;grid-template-columns:380px 1fr;min-height:calc(100vh - 62px)}
    aside{padding:16px;background:#fff;border-right:1px solid #ddd;overflow:auto}
    #map{min-height:calc(100vh - 62px)}
    form{display:flex;gap:8px}input{flex:1;padding:11px;border:1px solid #ccc;border-radius:10px}
    button{padding:11px 14px;border:0;border-radius:10px;background:#173f2b;color:#fff;font-weight:700;cursor:pointer}
    .result{display:block;width:100%;margin-top:8px;text-align:left;background:#eef3ef;color:#17211b}
    .card{background:#eef3ef;border-radius:10px;padding:10px 12px;margin-top:8px}
    .section{margin-top:20px;padding-top:16px;border-top:1px solid #ddd}
    .zone{font-size:24px;font-weight:800;color:#173f2b}
    .small{font-size:13px;color:#68726c;line-height:1.45}
    @media(max-width:800px){.layout{grid-template-columns:1fr;grid-template-rows:auto 65vh}#map{min-height:65vh}}
  </style>
</head>
<body>
<header><strong>Cyprus DLS Site Explorer</strong></header>
<div class="layout">
  <aside>
    <form id="searchForm">
      <input id="searchInput" placeholder="Search an address in Cyprus">
      <button>Search</button>
    </form>
    <div id="results"></div>

    <div class="section">
      <h3>Parcel</h3>
      <div id="parcel">Click a parcel on the map.</div>
    </div>

    <div class="section">
      <h3>Planning zone</h3>
      <div id="zones">No parcel selected.</div>
    </div>
  </aside>
  <div id="map"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/esri-leaflet@3.0.15/dist/esri-leaflet.js"></script>
<script>
const DLS="https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer";
const map=L.map("map").setView([35.1264,33.4299],9);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:20,attribution:"&copy; OpenStreetMap"}).addTo(map);
L.esri.dynamicMapLayer({url:DLS,layers:[0],opacity:1,minZoom:15}).addTo(map);

let selected=null;

map.on("click", async e=>{
  if(map.getZoom()<15){alert("Zoom in further before selecting a parcel.");return;}
  const r=await fetch(`/api/site?lat=${e.latlng.lat}&lon=${e.latlng.lng}`);
  const data=await r.json();
  if(!r.ok){alert(data.detail||"Lookup failed");return;}

  if(selected) map.removeLayer(selected);
  selected=L.geoJSON(data.parcel_feature,{
    style:{color:"#ff7a00",weight:4,fillColor:"#ffb15c",fillOpacity:.28}
  }).addTo(map);
  map.fitBounds(selected.getBounds(),{padding:[25,25],maxZoom:19});

  const p=data.parcel;
  document.getElementById("parcel").innerHTML=`
    <div class="card"><b>Parcel number:</b> ${p.parcel_number ?? "—"}</div>
    <div class="card"><b>Sheet:</b> ${p.sheet ?? "—"}</div>
    <div class="card"><b>Plan:</b> ${p.plan ?? "—"}</div>
    <div class="card"><b>District code:</b> ${p.district_code ?? "—"}</div>
    <div class="card"><b>Community code:</b> ${p.community_code ?? "—"}</div>
  `;

  document.getElementById("zones").innerHTML = data.zones.length
    ? data.zones.map(z=>`
        <div class="card">
          <div class="zone">${z.name ?? z.code ?? "Unknown zone"}</div>
          <div class="small">${z.description ?? ""}</div>
        </div>`).join("")
    : "No zone returned.";
});

document.getElementById("searchForm").addEventListener("submit", async e=>{
  e.preventDefault();
  const q=document.getElementById("searchInput").value.trim();
  if(!q)return;
  const r=await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  const data=await r.json();
  const box=document.getElementById("results");
  box.innerHTML="";
  (data.results||[]).forEach(x=>{
    const b=document.createElement("button");
    b.className="result";
    b.type="button";
    b.textContent=x.display_name;
    b.onclick=()=>map.setView([x.lat,x.lon],18);
    box.appendChild(b);
  });
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def homepage() -> HTMLResponse:
    return HTMLResponse(HTML)
