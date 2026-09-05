/* CryoLens research dashboard. Server data is always inserted as text. */
'use strict';

document.addEventListener('DOMContentLoaded', () => {
  const $ = (id) => document.getElementById(id);
  let selectedScene = null;
  let detections = [];
  let selectedTarget = null;
  let requestVersion = 0;
  let writeEnabled = false;
  let reviewBusy = false;
  let toastTimer;
  let scenePartial = false;
  let detectionPartial = false;
  const drawer = $('target-drawer');
  const reviewButtons = [...document.querySelectorAll('[data-verdict]')];

  function message(text) {
    $('map-message').textContent = text;
    $('map-message').hidden = !text;
  }
  function toast(text) {
    clearTimeout(toastTimer);
    $('toast').textContent = text;
    $('toast').hidden = false;
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 6500);
  }
  if (typeof L === 'undefined') {
    message('The map library could not load. Check your connection to the public Leaflet CDN. The API documentation remains available.');
    $('api-status').textContent = 'Map unavailable';
    $('scene-select').replaceChildren(new Option('Map library unavailable', ''));
    return;
  }

  const map = L.map('map', { center: [53.1, -55.5], zoom: 5, minZoom: 4, maxZoom: 16 });
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  const footprint = L.geoJSON(null, { style: { color: '#75bdcf', weight: 1.5, fillOpacity: 0.05, dashArray: '5 5' } }).addTo(map);
  const targets = L.layerGroup().addTo(map);
  const observations = L.layerGroup().addTo(map);

  async function request(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal });
      if (!response.ok) {
        let detail = `Service returned HTTP ${response.status}`;
        try {
          const body = await response.json();
          if (typeof body.detail === 'string') detail = body.detail;
          else if (Array.isArray(body.detail)) detail = body.detail.map((e) => e.msg).join('; ');
        } catch (_) { /* Use the HTTP status when a proxy returns HTML. */ }
        throw new Error(detail);
      }
      return await response.json();
    } finally { clearTimeout(timer); }
  }

  async function collect(path, params, maximum) {
    const features = [];
    let offset = 0;
    for (let page = 0; page < 25; page++) {
      const query = new URLSearchParams({ ...params, offset: String(offset) });
      const data = await request(`${path}?${query}`);
      if (!Array.isArray(data.features)) throw new Error('Service returned an invalid observation collection.');
      features.push(...data.features);
      if (data.next_offset === null || data.next_offset === undefined) return { features, partial: false };
      if (features.length >= maximum) return { features: features.slice(0, maximum), partial: true };
      if (data.next_offset <= offset) throw new Error('Service returned invalid pagination.');
      offset = data.next_offset;
    }
    return { features, partial: true };
  }

  const when = (value) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toISOString().replace('T', ' ').replace('.000Z', ' UTC') : 'Unknown';
  const number = (value, suffix, digits = 1) => typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(digits)}${suffix}` : 'Unknown';
  function pointOf(feature) {
    const coords = feature.properties?.centroid || (feature.geometry?.type === 'Point' ? feature.geometry.coordinates : null);
    return coords && coords.length >= 2 && coords.every(Number.isFinite) ? [coords[1], coords[0]] : null;
  }
  const confirmed = (p) => p.analyst_verdict === 'CONFIRMED_ICEBERG' && p.validated;
  const assessment = (p) => p.validated ? (confirmed(p) ? 'Analyst-confirmed iceberg' : `Analyst review: ${(p.analyst_verdict || 'unknown').replaceAll('_', ' ').toLowerCase()}`) : 'Unverified SAR candidate';

  function closeDrawer() {
    selectedTarget = null;
    drawer.hidden = true;
  }
  function render() {
    targets.clearLayers();
    const filter = $('assessment-filter').value;
    const minimum = Number($('score-filter').value);
    let visible = 0;
    let selectionVisible = false;
    for (const feature of detections) {
      const p = feature.properties;
      if (filter === 'confirmed' && !confirmed(p)) continue;
      if (filter === 'candidate' && p.validated) continue;
      if (filter === 'reviewed' && !p.validated) continue;
      if (minimum > 0 && (typeof p.confidence !== 'number' || p.confidence < minimum)) continue;
      const location = pointOf(feature);
      if (!location) continue;
      const color = confirmed(p) ? '#87dddb' : p.validated ? '#9eb2c3' : '#edc279';
      const marker = L.circleMarker(location, { radius: confirmed(p) ? 7 : 5, weight: 1.5, color, fillColor: color, fillOpacity: p.validated ? 0.7 : 0.15 });
      const tooltip = document.createElement('span');
      tooltip.textContent = `${assessment(p)} · ${String(p.id).slice(0, 8)}`;
      marker.bindTooltip(tooltip);
      marker.on('click', () => inspect(feature));
      marker.addTo(targets);
      visible++;
      if (selectedTarget?.properties.id === p.id) selectionVisible = true;
    }
    $('count-visible').textContent = String(visible);
    $('count-candidates').textContent = String(detections.filter((f) => !f.properties.validated).length);
    $('count-confirmed').textContent = String(detections.filter((f) => confirmed(f.properties)).length);
    if (selectedTarget && !selectionVisible) closeDrawer();
    if (visible) message(detectionPartial ? 'Showing a bounded subset of this scene. Use the paginated API for the complete review set.' : '');
    else if (!detections.length) message('No stored candidates for this observation. This does not establish that the area is iceberg-free.');
    else if (filter === 'confirmed') message('No analyst-confirmed icebergs meet this filter. Select “Unverified SAR candidates” to inspect the pending radar evidence.');
    else message('No targets meet the current review and score filters.');
  }

  async function loadScene(scene) {
    const version = ++requestVersion;
    selectedScene = scene;
    detections = [];
    targets.clearLayers();
    observations.clearLayers();
    footprint.clearLayers();
    closeDrawer();
    for (const id of ['count-visible', 'count-candidates', 'count-confirmed']) $(id).textContent = '—';
    $('meta-time').textContent = when(scene.properties.acquisition_time);
    $('meta-platform').textContent = scene.properties.platform || 'Unknown';
    $('meta-mode').textContent = `${scene.properties.mode || 'Unknown'} / ${(scene.properties.polarizations || []).join('+') || 'Unknown'}`;
    footprint.addData(scene);
    const bounds = footprint.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 60], maxZoom: 9 });
    message('Loading candidates for the selected acquisition…');
    loadIip(scene, version);
    try {
      const data = await collect('/api/v1/detections', { scene_id: scene.properties.id, limit: '1000' }, 10000);
      if (version !== requestVersion) return;
      detections = data.features;
      detectionPartial = data.partial;
      render();
    } catch (error) {
      if (version !== requestVersion) return;
      message(`Candidates unavailable. ${error.message}`);
    }
  }

  async function loadIip(scene, version) {
    const acquisition = Date.parse(scene.properties.acquisition_time);
    if (!Number.isFinite(acquisition)) {
      $('data-status').textContent = 'IIP context unavailable: acquisition time unknown';
      return;
    }
    $('data-status').textContent = 'Loading IIP observations within ±12 hours…';
    try {
      const data = await collect('/api/v1/iip', {
        start_date: new Date(acquisition - 12 * 3600000).toISOString(),
        end_date: new Date(acquisition + 12 * 3600000).toISOString(), limit: '1000',
      }, 5000);
      if (version !== requestVersion) return;
      for (const feature of data.features) {
        const location = pointOf(feature);
        if (!location) continue;
        const p = feature.properties;
        const tooltip = document.createElement('span');
        tooltip.textContent = `IIP observation · ${when(p.sighting_time)} · ${p.size_class || 'Unknown size'} · Association is unverified`;
        L.circleMarker(location, { radius: 5, color: '#c5a4e5', fillOpacity: 0.4, weight: 1.5 }).bindTooltip(tooltip).addTo(observations);
      }
      $('data-status').textContent = `IIP context: ${data.features.length}${data.partial ? '+' : ''} stored observations within ±12h${scenePartial ? ' · Scene list is partial' : ''}`;
    } catch (error) {
      if (version !== requestVersion) return;
      $('data-status').textContent = `IIP context unavailable: ${error.message}`;
    }
  }

  function inspect(feature) {
    selectedTarget = feature;
    const p = feature.properties;
    const location = pointOf(feature);
    $('drawer-id').textContent = `Target ${String(p.id).slice(0, 8)}`;
    $('target-assessment').textContent = assessment(p);
    $('target-observed').textContent = `Observed: ${when(p.observation_time)}`;
    $('target-detector').textContent = `${p.detector_name || 'Unknown'} / ${p.predicted_class || 'Unknown'}`;
    $('target-score').textContent = number(p.confidence, '', 3);
    $('target-hv').textContent = `${number(p.peak_sigma0_hv_db, ' dB')} / ${number(p.mean_sigma0_hv_db, ' dB')}`;
    $('target-hh').textContent = `${number(p.peak_sigma0_hh_db, ' dB')} / ${number(p.hh_hv_ratio_db, ' dB')}`;
    $('target-size').textContent = `${number(p.length_m, ' m')} × ${number(p.width_m, ' m')}`;
    $('target-area').textContent = number(p.estimated_area_m2, ' m²', 0);
    $('target-position').textContent = location ? `${location[0].toFixed(4)}°, ${location[1].toFixed(4)}°` : 'Unknown';
    $('target-iip').textContent = p.iip_association ? 'Proximity association; unverified' : 'No stored association';
    $('review-history').textContent = p.validated ? `${assessment(p)} by ${p.analyst_id || 'unknown analyst'} at ${when(p.validated_at)}. ${p.review_notes || 'No evidence notes stored.'}` : 'No analyst review recorded.';
    $('review-notes').value = '';
    updateReviewAccess();
    drawer.hidden = false;
    $('drawer-close').focus({ preventScroll: true });
  }

  function updateReviewAccess() {
    $('review-access').textContent = writeEnabled ? 'A configured analyst key is required. The server records its assigned analyst identity.' : 'Read-only portfolio. Analyst writes are disabled on the server.';
    reviewButtons.forEach((button) => { button.disabled = !writeEnabled || reviewBusy; });
    $('analyst-key').disabled = !writeEnabled || reviewBusy;
    $('review-notes').disabled = !writeEnabled || reviewBusy;
  }

  async function review(verdict) {
    if (!selectedTarget || reviewBusy || !writeEnabled) return;
    const id = selectedTarget.properties.id;
    const version = requestVersion;
    const key = $('analyst-key').value;
    const notes = $('review-notes').value.trim();
    if (!key || notes.length < 10) { toast('Provide the analyst key and at least 10 characters describing review evidence.'); return; }
    reviewBusy = true;
    updateReviewAccess();
    try {
      await request(`/api/v1/detections/${encodeURIComponent(id)}/validate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Analyst-Key': key },
        body: JSON.stringify({ analyst_verdict: verdict, notes }),
      });
      const refreshed = await request(`/api/v1/detections/${encodeURIComponent(id)}`);
      if (version === requestVersion) {
        detections = detections.map((f) => f.properties.id === id ? refreshed : f);
        const stillSelected = selectedTarget?.properties.id === id;
        render();
        if (stillSelected && selectedTarget) inspect(refreshed);
      }
      toast('Analyst review saved. The raw detector result remains preserved.');
    } catch (error) { toast(`Review could not be completed: ${error.message}`); }
    finally { reviewBusy = false; updateReviewAccess(); }
  }

  $('scene-select').addEventListener('change', (event) => {
    const scene = event.target.selectedOptions[0]?.scene;
    if (scene) loadScene(scene);
  });
  $('assessment-filter').addEventListener('change', render);
  $('score-filter').addEventListener('input', (event) => { $('score-value').textContent = Number(event.target.value).toFixed(2); render(); });
  $('toggle-footprint').addEventListener('change', (event) => { if (event.target.checked) footprint.addTo(map); else map.removeLayer(footprint); });
  $('toggle-iip').addEventListener('change', (event) => { if (event.target.checked) observations.addTo(map); else map.removeLayer(observations); });
  $('drawer-close').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDrawer(); });
  reviewButtons.forEach((button) => button.addEventListener('click', () => review(button.dataset.verdict)));

  async function initialize() {
    request('/health').then((data) => {
      $('api-status').textContent = data.status === 'healthy' ? 'Database connected' : 'Database unavailable';
    }).catch(() => { $('api-status').textContent = 'Service unavailable'; });
    try {
      const capabilities = await request('/api/v1/capabilities');
      writeEnabled = capabilities.analyst_writes_enabled === true;
      updateReviewAccess();
      const studyArea = L.geoJSON(capabilities.aoi, { style: { color: '#6895a3', weight: 1, dashArray: '6 5', fill: false }, interactive: false }).addTo(map);
      const bounds = studyArea.getBounds();
      if (bounds.isValid()) { map.fitBounds(bounds, { padding: [24, 24] }); map.setMaxBounds(bounds.pad(0.2)); }
      const data = await collect('/api/v1/scenes', { limit: '100' }, 500);
      scenePartial = data.partial;
      $('scene-select').replaceChildren();
      if (!data.features.length) {
        $('scene-select').append(new Option('No processed NL scenes', ''));
        for (const id of ['count-visible', 'count-candidates', 'count-confirmed']) $(id).textContent = '0';
        message('No processed observations are available in the NL study area. Ingest and process an authentic scene to begin review. No demonstration detections are substituted.');
        $('data-status').textContent = 'IIP context: no scene selected';
        return;
      }
      for (const scene of data.features) {
        const option = new Option(`${when(scene.properties.acquisition_time).slice(0, 16)} · ${scene.properties.platform} · ${scene.properties.detection_count} candidates`, scene.properties.id);
        option.scene = scene;
        $('scene-select').append(option);
      }
      await loadScene(data.features[0]);
    } catch (error) {
      $('scene-select').replaceChildren(new Option('Observation service unavailable', ''));
      message(`Cannot load stored observations. ${error.message}`);
      $('data-status').textContent = 'Observation service unavailable';
    }
  }
  initialize();
});
