/**
 * CryoLens — Maritime Domain Awareness Leaflet Controller
 */

document.addEventListener('DOMContentLoaded', () => {
  // State
  let map;
  let sceneFootprintsLayer;
  let detectionsLayer;
  let aoiLayer;
  let iipLayer;
  let driftLayer;
  let currentDetections = [];
  let currentIIPSightings = [];
  let selectedTarget = null;
  let currentSceneId = null;

  // DOM Elements
  const apiStatusEl = document.getElementById('api-status');
  const hudTotalCountEl = document.getElementById('hud-total-count');
  const sceneSelectEl = document.getElementById('scene-select');
  const metaPlatformEl = document.getElementById('meta-platform');
  const metaTimeEl = document.getElementById('meta-time');
  const metaModeEl = document.getElementById('meta-mode');
  const confidenceSlider = document.getElementById('confidence-slider');
  const confidenceValEl = document.getElementById('confidence-val');
  const filterIceberg = document.getElementById('filter-iceberg');
  const filterShip = document.getElementById('filter-ship');
  const filterClutter = document.getElementById('filter-clutter');
  const countIcebergEl = document.getElementById('count-iceberg');
  const countShipEl = document.getElementById('count-ship');
  const countClutterEl = document.getElementById('count-clutter');
  const statValidatedEl = document.getElementById('stat-validated');
  const statPendingEl = document.getElementById('stat-pending');
  const toggleFootprint = document.getElementById('toggle-footprint');
  const toggleAoi = document.getElementById('toggle-aoi');
  const toggleIip = document.getElementById('toggle-iip');
  const filterIipCorrelated = document.getElementById('filter-iip-correlated');
  const detectorCfar = document.getElementById('detector-cfar');
  const detectorYolo = document.getElementById('detector-yolo');

  // Drawer Elements
  const targetDrawer = document.getElementById('target-drawer');
  const drawerCloseBtn = document.getElementById('drawer-close');
  const drawerIconEl = document.getElementById('drawer-icon');
  const drawerIdEl = document.getElementById('drawer-id');
  const drawerClassBadgeEl = document.getElementById('drawer-class-badge');
  const tPeakHvEl = document.getElementById('t-peak-hv');
  const tMeanHvEl = document.getElementById('t-mean-hv');
  const tPeakHhEl = document.getElementById('t-peak-hh');
  const tRatioEl = document.getElementById('t-ratio');
  const tIncEl = document.getElementById('t-inc');
  const tLengthEl = document.getElementById('t-length');
  const tWidthEl = document.getElementById('t-width');
  const tAreaEl = document.getElementById('t-area');
  const tCoordsEl = document.getElementById('t-coords');
  const tValidationStatusEl = document.getElementById('t-validation-status');
  const btnValidateIceberg = document.getElementById('btn-validate-iceberg');
  const btnValidateShip = document.getElementById('btn-validate-ship');
  const btnValidateClutter = document.getElementById('btn-validate-clutter');
  const toastEl = document.getElementById('toast');

  // 1. Initialize Map
  function initMap() {
    map = L.map('map', {
      center: [47.5, -52.0],
      zoom: 6,
      zoomControl: true,
      attributionControl: false,
    });

    // Dark Matter basemap
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
      maxZoom: 18,
      subdomains: 'abcd',
    }).addTo(map);

    sceneFootprintsLayer = L.geoJSON(null, {
      style: {
        color: '#00E5FF',
        weight: 2,
        dashArray: '4, 4',
        fillColor: '#00E5FF',
        fillOpacity: 0.08,
      },
    }).addTo(map);

    detectionsLayer = L.layerGroup().addTo(map);

    // Operational Grand Banks AOI Box [-60.0, 43.5, -46.0, 55.0]
    const aoiBounds = [[43.5, -60.0], [55.0, -46.0]];
    aoiLayer = L.rectangle(aoiBounds, {
      color: '#48CAE4',
      weight: 1,
      dashArray: '8, 8',
      fill: false,
    }).addTo(map);

    iipLayer = L.layerGroup().addTo(map);
    driftLayer = L.layerGroup().addTo(map);
  }

  // Toast Helper
  function showToast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.remove('hidden');
    setTimeout(() => {
      toastEl.classList.add('hidden');
    }, 3000);
  }

  // 2. Fetch Health
  async function checkHealth() {
    try {
      const res = await fetch('/health');
      if (res.ok) {
        const data = await res.json();
        apiStatusEl.textContent = data.status === 'healthy' ? 'ACTIVE' : 'DEGRADED';
        apiStatusEl.parentElement.querySelector('.dot').className = 'dot live';
      } else {
        apiStatusEl.textContent = 'OFFLINE';
      }
    } catch (e) {
      apiStatusEl.textContent = 'DISCONNECTED';
    }
  }

  // 3. Load Scenes
  async function loadScenes() {
    try {
      const res = await fetch('/api/v1/scenes');
      const data = await res.json();

      sceneSelectEl.innerHTML = '';
      if (!data.features || data.features.length === 0) {
        sceneSelectEl.innerHTML = '<option value="">No processed scenes found</option>';
        return;
      }

      sceneFootprintsLayer.clearLayers();
      sceneFootprintsLayer.addData(data);

      data.features.forEach((feat, idx) => {
        const opt = document.createElement('option');
        opt.value = feat.properties.id;
        opt.textContent = `${feat.properties.product_id.substring(0, 32)}... (${feat.properties.detection_count} targets)`;
        opt.dataset.meta = JSON.stringify(feat.properties);
        sceneSelectEl.appendChild(opt);

        if (idx === 0) {
          currentSceneId = feat.properties.id;
          updateSceneMeta(feat.properties);
        }
      });

      if (currentSceneId) {
        await loadDetections(currentSceneId);
      }
    } catch (err) {
      console.error('Failed to load scenes:', err);
    }
  }

  function updateSceneMeta(meta) {
    metaPlatformEl.textContent = meta.platform || 'Sentinel-1';
    metaTimeEl.textContent = meta.acquisition_time ? new Date(meta.acquisition_time).toUTCString() : '--';
    metaModeEl.textContent = `${meta.mode || 'EW'} / ${(meta.polarizations || []).join('+')}`;
  }

  // 4. Load Detections
  async function loadDetections(sceneId) {
    try {
      const res = await fetch(`/api/v1/detections?scene_id=${sceneId}&limit=2000`);
      const data = await res.json();
      currentDetections = data.features || [];
      renderDetections();
    } catch (err) {
      console.error('Failed to load detections:', err);
    }
  }

  // Fetch IIP Sightings
  async function loadIIPSightings() {
    try {
      const res = await fetch('/api/v1/iip?limit=1000');
      const data = await res.json();
      currentIIPSightings = data.features || [];
      renderIIPSightings();
    } catch (err) {
      console.error('Failed to load IIP sightings:', err);
    }
  }

  function renderIIPSightings() {
    iipLayer.clearLayers();
    
    currentIIPSightings.forEach((feat) => {
      const p = feat.properties;
      const lng = feat.geometry.coordinates[0];
      const lat = feat.geometry.coordinates[1];
      
      // IIP Circle Marker
      const marker = L.circleMarker([lat, lng], {
        radius: 6,
        color: '#FFA500', // Orange
        weight: 2,
        fillColor: '#FFA500',
        fillOpacity: 0.5,
      });
      
      marker.bindTooltip(`<b>IIP Sighting</b><br>Time: ${p.sighting_time}<br>Size: ${p.size_class || 'Unknown'}<br>Shape: ${p.shape || 'Unknown'}`);
      
      // Drift buffer (approx 12h at 0.5 m/s = ~21.6km)
      // Visual aid
      const buffer = L.circle([lat, lng], {
        radius: 21600, // meters
        color: '#FFA500',
        weight: 1,
        dashArray: '4, 4',
        fill: false,
      });

      iipLayer.addLayer(marker);
      iipLayer.addLayer(buffer);
    });
  }

  // 5. Render Detections on Map
  function renderDetections() {
    detectionsLayer.clearLayers();

    const minConf = parseFloat(confidenceSlider.value);
    const showIce = filterIceberg.checked;
    const showShip = filterShip.checked;
    const showClutter = filterClutter.checked;

    let iceCount = 0;
    let shipCount = 0;
    let clutterCount = 0;
    let validatedCount = 0;
    let visibleCount = 0;

    currentDetections.forEach((feat) => {
      const p = feat.properties;
      const cls = p.predicted_class;
      const conf = p.confidence;

      if (cls === 'iceberg') iceCount++;
      if (cls === 'ship') shipCount++;
      if (cls === 'clutter') clutterCount++;
      if (p.validated) validatedCount++;

      // Source check
      const showYolo = detectorYolo.checked;
      const isYolo = p.detector_name.toLowerCase().includes('yolo');
      if (showYolo && !isYolo) return;
      if (!showYolo && isYolo) return;

      // Filter check
      if (conf < minConf) return;
      if (cls === 'iceberg' && !showIce) return;
      if (cls === 'ship' && !showShip) return;
      if (cls === 'clutter' && !showClutter) return;
      
      if (filterIipCorrelated.checked && !p.IIP_CORRELATED) return;

      visibleCount++;

      // Centroid coordinates
      let lat, lng;
      if (feat.geometry.type === 'Point') {
        lng = feat.geometry.coordinates[0];
        lat = feat.geometry.coordinates[1];
      } else if (feat.geometry.type === 'Polygon') {
        const ring = feat.geometry.coordinates[0];
        lng = ring.reduce((sum, c) => sum + c[0], 0) / ring.length;
        lat = ring.reduce((sum, c) => sum + c[1], 0) / ring.length;
      }

      if (!lat || !lng) return;

      // Icon & Marker
      const iconChar = cls === 'iceberg' ? '🧊' : (cls === 'ship' ? '🚢' : '🌊');
      const pinClass = cls === 'ship' ? 'marker-pin ship' : (cls === 'clutter' ? 'marker-pin clutter' : 'marker-pin');

      const customIcon = L.divIcon({
        className: 'target-marker-icon',
        html: `<div class="${pinClass}">${iconChar}</div>`,
        iconSize: [28, 28],
        iconAnchor: [14, 14],
      });

      const marker = L.marker([lat, lng], { icon: customIcon });
      marker.on('click', () => openTargetDrawer(feat, [lat, lng]));
      detectionsLayer.addLayer(marker);
    });

    // Update HUD counters
    hudTotalCountEl.textContent = visibleCount;
    countIcebergEl.textContent = iceCount;
    countShipEl.textContent = shipCount;
    countClutterEl.textContent = clutterCount;
    statValidatedEl.textContent = validatedCount;
    statPendingEl.textContent = currentDetections.length - validatedCount;
  }

  // 6. Open Target Inspector Drawer
  function openTargetDrawer(feature, coords) {
    selectedTarget = feature;
    const p = feature.properties;
    
    // Clear previous drift forecasts
    driftLayer.clearLayers();

    drawerIconEl.textContent = p.predicted_class === 'iceberg' ? '🧊' : (p.predicted_class === 'ship' ? '🚢' : '🌊');
    drawerIdEl.textContent = `Target #${p.id.substring(0, 8)}`;
    drawerClassBadgeEl.textContent = p.predicted_class.toUpperCase();

    tPeakHvEl.textContent = `${p.peak_sigma0_hv_db ?? '--'} dB`;
    tMeanHvEl.textContent = `${p.mean_sigma0_hv_db ?? '--'} dB`;
    tPeakHhEl.textContent = `${p.peak_sigma0_hh_db ?? '--'} dB`;
    tRatioEl.textContent = `${p.hh_hv_ratio_db ?? '--'} dB`;
    tIncEl.textContent = `${p.incidence_angle_deg ?? '--'}°`;

    tLengthEl.textContent = `${p.length_m ?? '--'} m`;
    tWidthEl.textContent = `${p.width_m ?? '--'} m`;
    tAreaEl.textContent = `${p.estimated_area_m2 ?? '--'} m²`;
    tCoordsEl.textContent = `${coords[0].toFixed(4)}°N, ${Math.abs(coords[1]).toFixed(4)}°W`;

    if (p.validated) {
      tValidationStatusEl.innerHTML = `Status: <span class="status-tag confirmed">✓ ${p.analyst_verdict}</span>`;
    } else {
      tValidationStatusEl.innerHTML = `Status: <span class="status-tag pending">UNVALIDATED</span>`;
    }
    
    if (p.IIP_CORRELATED) {
       tValidationStatusEl.innerHTML += `<div style="margin-top: 5px; color: #FFA500; font-size: 0.85rem;">[IIP Correlated]</div>`;
    }

    targetDrawer.classList.remove('hidden');
    
    // Fetch and display drift forecast if it's an iceberg
    if (p.predicted_class === 'iceberg') {
       fetchDriftForecast(p.id);
    }
  }

  // Fetch Drift Forecast
  async function fetchDriftForecast(detectionId) {
    try {
      const res = await fetch(`/api/v1/drift/${detectionId}`);
      const data = await res.json();
      
      if (data.features && data.features.length > 0) {
         L.geoJSON(data, {
            style: {
               color: '#FF00FF', // Magenta for drift
               weight: 3,
               opacity: 0.8,
               dashArray: '5, 5'
            },
            onEachFeature: function (feature, layer) {
                // Try to put a small tooltip at the end
                if (feature.geometry.coordinates && feature.geometry.coordinates.length > 0) {
                    const times = feature.properties.times;
                    if (times && times.length > 0) {
                        const lastTime = new Date(times[times.length-1]);
                        layer.bindTooltip(`<b>Drift Forecast</b><br>+${times.length} hours<br>Until: ${lastTime.toUTCString()}`);
                    }
                }
            }
         }).addTo(driftLayer);
      }
    } catch (err) {
      console.error('Failed to fetch drift forecast:', err);
    }
  }

  // 7. Submit Validation
  async function submitValidation(verdict, correctedClass = null) {
    if (!selectedTarget) return;

    try {
      const res = await fetch(`/api/v1/detections/${selectedTarget.properties.id}/validate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          analyst_verdict: verdict,
          corrected_class: correctedClass || selectedTarget.properties.predicted_class,
          analyst_id: 'c-core_analyst_1',
          notes: 'Validated via interactive Leaflet operations dashboard',
        }),
      });

      if (res.ok) {
        showToast(`Ground truth recorded: ${verdict}`);
        selectedTarget.properties.validated = true;
        selectedTarget.properties.analyst_verdict = verdict;
        if (correctedClass) selectedTarget.properties.predicted_class = correctedClass;

        tValidationStatusEl.innerHTML = `Status: <span class="status-tag confirmed">✓ ${verdict}</span>`;
        renderDetections();
      }
    } catch (e) {
      showToast('Validation failed to save');
    }
  }

  // Event Listeners
  sceneSelectEl.addEventListener('change', (e) => {
    currentSceneId = e.target.value;
    const selectedOption = e.target.selectedOptions[0];
    if (selectedOption && selectedOption.dataset.meta) {
      updateSceneMeta(JSON.parse(selectedOption.dataset.meta));
    }
    if (currentSceneId) {
      loadDetections(currentSceneId);
    }
  });

  confidenceSlider.addEventListener('input', (e) => {
    confidenceValEl.textContent = parseFloat(e.target.value).toFixed(2);
    renderDetections();
  });

  filterIceberg.addEventListener('change', renderDetections);
  filterShip.addEventListener('change', renderDetections);
  filterClutter.addEventListener('change', renderDetections);

  toggleFootprint.addEventListener('change', (e) => {
    if (e.target.checked) map.addLayer(sceneFootprintsLayer);
    else map.removeLayer(sceneFootprintsLayer);
  });

  toggleAoi.addEventListener('change', (e) => {
    if (e.target.checked) map.addLayer(aoiLayer);
    else map.removeLayer(aoiLayer);
  });

  toggleIip.addEventListener('change', (e) => {
    if (e.target.checked) map.addLayer(iipLayer);
    else map.removeLayer(iipLayer);
  });

  filterIipCorrelated.addEventListener('change', renderDetections);
  detectorCfar.addEventListener('change', renderDetections);
  detectorYolo.addEventListener('change', renderDetections);

  drawerCloseBtn.addEventListener('click', () => {
    targetDrawer.classList.add('hidden');
    selectedTarget = null;
    driftLayer.clearLayers();
  });

  btnValidateIceberg.addEventListener('click', () => submitValidation('CONFIRMED_ICEBERG', 'iceberg'));
  btnValidateShip.addEventListener('click', () => submitValidation('VESSEL', 'ship'));
  btnValidateClutter.addEventListener('click', () => submitValidation('REJECTED_CLUTTER', 'clutter'));

  // Init
  initMap();
  checkHealth();
  loadScenes();
  loadIIPSightings();
});
