import json
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="Cyprus DLS Parcel Map Test", version="0.1.0")

DLS_MAPSERVER = (
    "https://eservices.dls.moi.gov.cy/arcgis/rest/services/"
    "National/CadastralMap_EN/MapServer"
)
DLS_PARCEL_QUERY = f"{DLS_MAPSERVER}/0/query"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"

# Small in-memory cache for user-triggered address searches.
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
        "User-Agent": "CyprusDLSMapPrototype/0.1 (parcel-map-test)",
        "Accept-Language": "en,el;q=0.8",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            NOMINATIM_SEARCH,
            params=params,
            headers=headers,
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Address search failed with status {response.status_code}.",
        )

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


@app.get("/api/parcel")
async def parcel(
    lat: float = Query(ge=34.0, le=36.0),
    lon: float = Query(ge=31.0, le=35.0),
) -> dict[str, Any]:
    # ArcGIS can accept the click point in WGS84 while returning parcel geometry
    # in WGS84/GeoJSON for direct display in Leaflet.
    params = {
        "f": "geojson",
        "where": "1=1",
        "geometry": json.dumps(
            {
                "x": lon,
                "y": lat,
                "spatialReference": {"wkid": 4326},
            }
        ),
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": (
            "OBJECTID,SBPI_ID_NO,DIST_CODE,VIL_CODE,QRTR_CODE,"
            "BLCK_CODE,PARCEL_NBR,SHEET,PLAN_NBR,SRC_SL_CODE"
        ),
        "returnGeometry": "true",
        "resultRecordCount": 5,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(DLS_PARCEL_QUERY, params=params)

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"DLS parcel query failed with status {response.status_code}.",
        )

    data = response.json()

    if "error" in data:
        raise HTTPException(
            status_code=502,
            detail=f"DLS error: {data['error']}",
        )

    features = data.get("features", [])

    if not features:
        raise HTTPException(
            status_code=404,
            detail="No DLS parcel was found at that point.",
        )

    feature = features[0]
    properties = feature.get("properties", {})

    return {
        "parcel": {
            "parcel_number": properties.get("PARCEL_NBR"),
            "sheet": properties.get("SHEET"),
            "plan": properties.get("PLAN_NBR"),
            "district_code": properties.get("DIST_CODE"),
            "community_code": properties.get("VIL_CODE"),
            "quarter_code": properties.get("QRTR_CODE"),
            "block_code": properties.get("BLCK_CODE"),
            "sbpi_id_no": properties.get("SBPI_ID_NO"),
            "object_id": properties.get("OBJECTID"),
            "source_scale_code": properties.get("SRC_SL_CODE"),
        },
        "feature": feature,
    }


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
  />
  <title>Cyprus DLS Parcel Map Test</title>

  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  />

  <style>
    * { box-sizing: border-box; }

    body {
      margin: 0;
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      background: #f3f4f2;
      color: #17211b;
    }

    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }

    header {
      padding: 16px 22px;
      background: white;
      border-bottom: 1px solid #dfe4e0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      z-index: 1000;
    }

    .eyebrow {
      font-size: 11px;
      letter-spacing: 0.14em;
      color: #68726c;
      font-weight: 800;
    }

    h1 {
      margin: 3px 0 0;
      font-size: 22px;
    }

    .status {
      color: #68726c;
      font-size: 13px;
    }

    .layout {
      min-height: 0;
      display: grid;
      grid-template-columns: 380px 1fr;
    }

    .sidebar {
      background: #fff;
      border-right: 1px solid #dfe4e0;
      padding: 18px;
      overflow-y: auto;
      z-index: 500;
    }

    .search-box {
      display: flex;
      gap: 8px;
    }

    input {
      width: 100%;
      border: 1px solid #cfd8d1;
      border-radius: 12px;
      padding: 12px 13px;
      font: inherit;
      outline: none;
    }

    button {
      border: 0;
      border-radius: 12px;
      background: #173f2b;
      color: white;
      font: inherit;
      font-weight: 700;
      padding: 11px 15px;
      cursor: pointer;
    }

    button:disabled {
      opacity: 0.55;
      cursor: wait;
    }

    .hint {
      color: #68726c;
      font-size: 13px;
      line-height: 1.5;
      margin: 12px 0 18px;
    }

    .results {
      display: grid;
      gap: 8px;
      margin-bottom: 20px;
    }

    .result {
      background: #f5f7f5;
      color: #17211b;
      border: 1px solid #dfe4e0;
      text-align: left;
      font-weight: 500;
      line-height: 1.4;
    }

    .panel {
      border-top: 1px solid #dfe4e0;
      padding-top: 18px;
    }

    .panel h2 {
      font-size: 17px;
      margin: 0 0 12px;
    }

    .empty {
      color: #68726c;
      line-height: 1.55;
      font-size: 14px;
    }

    .details {
      display: grid;
      gap: 10px;
    }

    .detail {
      padding: 11px 12px;
      background: #edf3ef;
      border-radius: 11px;
    }

    .detail-label {
      color: #68726c;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 800;
      margin-bottom: 3px;
    }

    .detail-value {
      font-weight: 700;
      word-break: break-word;
    }

    #map {
      width: 100%;
      height: 100%;
      min-height: calc(100vh - 78px);
      background: #dde4df;
    }

    .leaflet-control-layers {
      border-radius: 12px;
    }

    .map-note {
      position: absolute;
      left: 50%;
      bottom: 26px;
      transform: translateX(-50%);
      z-index: 700;
      background: rgba(23, 63, 43, 0.92);
      color: white;
      padding: 10px 14px;
      border-radius: 999px;
      font-size: 13px;
      pointer-events: none;
      box-shadow: 0 8px 24px rgba(0,0,0,.18);
    }

    .error {
      color: #9b2c2c;
      background: #fff0f0;
      border: 1px solid #e6c3c3;
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 13px;
      margin-top: 10px;
    }

    @media (max-width: 800px) {
      .layout {
        grid-template-columns: 1fr;
        grid-template-rows: auto 68vh;
      }

      .sidebar {
        border-right: 0;
        border-bottom: 1px solid #dfe4e0;
      }

      #map {
        min-height: 68vh;
      }
    }
  </style>
</head>

<body>
  <div class="app">
    <header>
      <div>
        <div class="eyebrow">DLS MAP PROTOTYPE</div>
        <h1>Cyprus Parcel Explorer</h1>
      </div>
      <div class="status">Official DLS parcel layer</div>
    </header>

    <div class="layout">
      <aside class="sidebar">
        <form id="searchForm" class="search-box">
          <input
            id="searchInput"
            placeholder="Search an address in Cyprus"
            autocomplete="off"
          />
          <button id="searchButton" type="submit">Search</button>
        </form>

        <p class="hint">
          Search for an address, zoom in, then click inside a DLS parcel.
          Parcel boundaries appear when you are sufficiently zoomed in.
        </p>

        <div id="results" class="results"></div>

        <section class="panel">
          <h2>Selected parcel</h2>
          <div id="parcelContent" class="empty">
            No parcel selected yet.
          </div>
          <div id="errorBox"></div>
        </section>
      </aside>

      <div style="position: relative; min-height: 0;">
        <div id="map"></div>
        <div class="map-note">
          Zoom in and click inside a parcel
        </div>
      </div>
    </div>
  </div>

  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
  ></script>
  <script
    src="https://unpkg.com/esri-leaflet@3.0.15/dist/esri-leaflet.js"
  ></script>

  <script>
    const DLS_MAPSERVER =
      "https://eservices.dls.moi.gov.cy/arcgis/rest/services/" +
      "National/CadastralMap_EN/MapServer";

    const map = L.map("map", {
      center: [35.1264, 33.4299],
      zoom: 9
    });

    const osm = L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        maxZoom: 20,
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">' +
          "OpenStreetMap contributors</a>"
      }
    ).addTo(map);

    const dlsParcels = L.esri.dynamicMapLayer({
      url: DLS_MAPSERVER,
      layers: [0],
      opacity: 1,
      minZoom: 15,
      f: "image"
    }).addTo(map);

    L.control.layers(
      { "OpenStreetMap": osm },
      { "DLS parcels": dlsParcels },
      { collapsed: false }
    ).addTo(map);

    let selectedLayer = null;
    let searchMarker = null;

    const searchForm = document.getElementById("searchForm");
    const searchInput = document.getElementById("searchInput");
    const searchButton = document.getElementById("searchButton");
    const resultsBox = document.getElementById("results");
    const parcelContent = document.getElementById("parcelContent");
    const errorBox = document.getElementById("errorBox");

    function clearError() {
      errorBox.innerHTML = "";
    }

    function showError(message) {
      errorBox.innerHTML =
        `<div class="error">${escapeHtml(message)}</div>`;
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function valueOrDash(value) {
      return value === null || value === undefined || value === ""
        ? "—"
        : String(value);
    }

    function renderParcel(parcel) {
      const rows = [
        ["Parcel number", parcel.parcel_number],
        ["Sheet", parcel.sheet],
        ["Plan", parcel.plan],
        ["District code", parcel.district_code],
        ["Community code", parcel.community_code],
        ["Quarter code", parcel.quarter_code],
        ["Block code", parcel.block_code],
        ["SBPI ID", parcel.sbpi_id_no]
      ];

      parcelContent.className = "details";
      parcelContent.innerHTML = rows
        .map(
          ([label, value]) => `
            <div class="detail">
              <div class="detail-label">${escapeHtml(label)}</div>
              <div class="detail-value">${escapeHtml(valueOrDash(value))}</div>
            </div>
          `
        )
        .join("");
    }

    async function selectParcel(lat, lon) {
      clearError();
      parcelContent.className = "empty";
      parcelContent.textContent = "Finding DLS parcel…";

      try {
        const response = await fetch(
          `/api/parcel?lat=${encodeURIComponent(lat)}` +
          `&lon=${encodeURIComponent(lon)}`
        );

        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.detail || "Parcel lookup failed.");
        }

        if (selectedLayer) {
          map.removeLayer(selectedLayer);
        }

        selectedLayer = L.geoJSON(data.feature, {
          style: {
            color: "#ff7a00",
            weight: 4,
            fillColor: "#ffb15c",
            fillOpacity: 0.28
          }
        }).addTo(map);

        map.fitBounds(selectedLayer.getBounds(), {
          padding: [30, 30],
          maxZoom: 19
        });

        renderParcel(data.parcel);
      } catch (error) {
        parcelContent.className = "empty";
        parcelContent.textContent = "No parcel selected.";
        showError(error.message);
      }
    }

    map.on("click", (event) => {
      if (map.getZoom() < 15) {
        showError("Zoom in further before selecting a parcel.");
        return;
      }

      selectParcel(event.latlng.lat, event.latlng.lng);
    });

    searchForm.addEventListener("submit", async (event) => {
      event.preventDefault();

      const query = searchInput.value.trim();
      if (!query) return;

      clearError();
      resultsBox.innerHTML = "";
      searchButton.disabled = true;
      searchButton.textContent = "Searching…";

      try {
        const response = await fetch(
          `/api/geocode?q=${encodeURIComponent(query)}`
        );

        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.detail || "Address search failed.");
        }

        if (!data.results.length) {
          resultsBox.innerHTML =
            '<div class="empty">No Cyprus address results found.</div>';
          return;
        }

        data.results.forEach((result) => {
          const button = document.createElement("button");
          button.className = "result";
          button.type = "button";
          button.textContent = result.display_name;

          button.addEventListener("click", () => {
            const lat = result.lat;
            const lon = result.lon;

            map.setView([lat, lon], 18);

            if (searchMarker) {
              map.removeLayer(searchMarker);
            }

            searchMarker = L.circleMarker([lat, lon], {
              radius: 7,
              color: "#173f2b",
              weight: 3,
              fillColor: "#ffffff",
              fillOpacity: 1
            }).addTo(map);
          });

          resultsBox.appendChild(button);
        });
      } catch (error) {
        showError(error.message);
      } finally {
        searchButton.disabled = false;
        searchButton.textContent = "Search";
      }
    });
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def homepage() -> HTMLResponse:
    return HTMLResponse(HTML)
