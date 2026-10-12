/* Pure search helpers are also exercised by Node, without a browser dependency. */
const ODSearch = (() => {
  const key = trip => JSON.stringify([trip.vehicle, trip.journey]);
  function coordinate(point) {
    if (!Array.isArray(point) || point.length !== 2 || !point.every(Number.isFinite) ||
        Math.abs(point[0]) > 90 || Math.abs(point[1]) > 180) throw Error('請輸入有效的緯度與經度。');
    return point;
  }
  function distance(a, b) {
    const rad = Math.PI / 180;
    const h = Math.sin((b[0] - a[0]) * rad / 2) ** 2 + Math.cos(a[0] * rad) *
      Math.cos(b[0] * rad) * Math.sin((b[1] - a[1]) * rad / 2) ** 2;
    return 12742000 * Math.asin(Math.sqrt(Math.min(1, Math.max(0, h))));
  }
  function find(trips, query) {
    coordinate(query.origin); coordinate(query.destination);
    if (!Number.isFinite(query.radius) || query.radius < 1 || query.radius > 100000)
      throw Error('容許距離請輸入 1～100,000 公尺。');
    return trips.filter(t => !query.screenedOnly || t.screened).map(t => ({...t,
      originGap: distance(query.origin, t.origin), destinationGap: distance(query.destination, t.destination)
    })).filter(t => t.originGap <= query.radius && t.destinationGap <= query.radius)
      .sort((a, b) => a.originGap + a.destinationGap - b.originGap - b.destinationGap || key(a).localeCompare(key(b)));
  }
  function summaries(trips) {
    const buckets = new Map();
    for (const trip of trips) {
      if (!buckets.has(trip.group)) buckets.set(trip.group, []);
      buckets.get(trip.group).push(trip);
    }
    const median = values => {
      values = values.filter(Number.isFinite).sort((a, b) => a - b);
      return values.length ? (values[Math.floor(values.length / 2)] + values[Math.floor((values.length - 1) / 2)]) / 2 : null;
    };
    return [...buckets].map(([group, rows]) => ({group, rows, count: rows.length,
      medianKm: median(rows.map(t => t.km)), medianFuel: median(rows.map(t => t.fuel)),
      medianEfficiency: median(rows.map(t => t.efficiency))})).sort((a, b) => Number(!a.group) - Number(!b.group) || a.group.localeCompare(b.group));
  }
  function csv(trips) {
    const cell = value => {
      if (typeof value === 'string' && /^[=+@\-\t\r]/.test(value)) value = "'" + value;
      return '"' + String(value ?? '').replaceAll('"', '""') + '"';
    };
    const rows = [['車輛','行程','出發時間','路線群組','起點距離 m','終點距離 m','里程 km','耗油 L','L/100km',
      '通過品質篩選','起點緯度','起點經度','終點緯度','終點經度'],
    ...trips.map(t => [t.vehicle,t.journey,t.start,t.group || '未分群',t.originGap.toFixed(1),t.destinationGap.toFixed(1),
      t.km,t.fuel,t.efficiency,t.screened,...t.origin,...t.destination])];
    return '\ufeff' + rows.map(row => row.map(cell).join(',')).join('\r\n');
  }
  return {key, coordinate, distance, find, summaries, csv};
})();
if (typeof module !== 'undefined' && module.exports) module.exports = ODSearch;

function mountODMap(data, doc = document, leaflet = globalThis.L) {
  const $ = id => doc.getElementById(id);
  if (!leaflet) {
    $('searchStatus').textContent = '無法載入 Leaflet 地圖程式，請檢查網路後重新整理。';
    $('searchStatus').classList.add('error');
    return;
  }
  const {key, find, summaries, csv} = ODSearch;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const format = (value, digits = 1) => Number.isFinite(value) ? value.toFixed(digits) : '—';
  const palette = ['#1769aa','#8e44ad','#00897b','#e65100','#5d4037','#c2185b'];
  const colors = new Map(Object.keys(data.groups).sort().map((g, i) => [g, palette[i % palette.length]]));
  const colorOf = trip => colors.get(trip.group) || '#667085';
  const map = leaflet.map('map', {preferCanvas:true, zoomControl:false}).setView([23.7,120.9],7);
  leaflet.control.zoom({position:'bottomleft'}).addTo(map);
  const tiles = leaflet.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom:19, attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  const tileStatus = doc.createElement('p'); tileStatus.className = 'muted tile-warning'; $('info').append(tileStatus);
  let tileFailed = false;
  tiles.on('loading', () => {tileFailed = false;});
  tiles.on('tileerror', () => {tileFailed = true; tileStatus.textContent = 'OpenStreetMap 圖磚暫時無法載入；請檢查網路，搜尋及 GPS 軌跡仍可使用。';});
  tiles.on('load', () => {if (!tileFailed) tileStatus.textContent = '';});
  const layer = leaflet.layerGroup().addTo(map), queryLayer = leaflet.layerGroup().addTo(map);
  let matches = [], query = null;
  const selected = new Set();
  const fields = ['originLat','originLon','destinationLat','destinationLon'];
  for (const trip of data.trips) $('seedTrip').add(new Option(`${trip.vehicle}｜${trip.start.replace('T',' ')}｜${format(trip.km)} km｜${trip.group || '未分群'}｜${trip.journey}`, key(trip)));
  const seedIndex = new Map(data.trips.map(t => [key(t), t]));
  function fillSeed() {
    const seed = seedIndex.get($('seedTrip').value);
    if (seed) fields.forEach((id, i) => {$(id).value = [...seed.origin, ...seed.destination][i];});
  }
  fillSeed();
  $('catalogStatus').textContent = `共 ${data.total.toLocaleString()} 趟；${data.trips.length.toLocaleString()} 趟起終點有效，${data.invalid} 趟座標缺漏／超出資料範圍。預設搜尋全部有效行程，含短於 10 km 的行程。`;
  const status = (text, error = false) => {$('searchStatus').textContent = text; $('searchStatus').classList.toggle('error', error);};
  function toggleSearch() {
    const open = $('searchPanel').hidden;
    $('searchPanel').hidden = !open;
    doc.body.classList.toggle('search-open', open);
    $('toggleSearch').textContent = open ? '收合搜尋' : '展開搜尋';
    $('toggleSearch').setAttribute('aria-expanded', String(open));
    map.invalidateSize();
  }
  function syncChecks() {
    const all = $('tripOptions').querySelector('input[data-all]');
    if (all) {all.checked = matches.length > 0 && selected.size === matches.length; all.indeterminate = selected.size > 0 && selected.size < matches.length;}
    $('tripOptions').querySelectorAll('input[data-trip]').forEach(input => {input.checked = selected.has(input.value);});
    $('tripSummary').textContent = `行程（${selected.size}/${matches.length} 已選）`;
  }
  function checkbox(text, value, all = false) {
    const label = doc.createElement('label'), input = doc.createElement('input');
    input.type = 'checkbox'; input.value = value; input.dataset[all ? 'all' : 'trip'] = 'true';
    label.append(input, doc.createTextNode(text)); $('tripOptions').append(label);
    input.addEventListener('change', () => {
      if (all) {selected.clear(); if (input.checked) matches.forEach(t => selected.add(key(t)));}
      else if (input.checked) selected.add(value); else selected.delete(value);
      syncChecks(); draw();
    });
  }
  function renderChecks() {
    $('tripOptions').replaceChildren();
    checkbox('全部符合行程', '', true);
    for (const t of matches) checkbox(`${t.vehicle}｜${t.journey}｜${t.start.replace('T',' ')}｜${format(t.km)} km｜${t.group || '未分群'}`, key(t));
    syncChecks();
  }
  function selectTrips(rows) {
    selected.clear(); rows.forEach(t => selected.add(key(t))); syncChecks(); draw();
  }
  function marker(point, color, label, popup) {
    return leaflet.circleMarker(point, {radius:7,color:'#fff',weight:2,fillColor:color,fillOpacity:1}).bindTooltip(label).bindPopup(popup).addTo(layer);
  }
  function draw() {
    layer.clearLayers(); const bounds = [];
    const shown = matches.filter(t => selected.has(key(t)));
    let stops = 0, tracks = 0;
    for (const t of shown) {
      const popup = `${escape(t.vehicle)} / ${escape(t.journey)}<br>${escape(t.start.replace('T',' '))}<br>${escape(t.group || '未分群')}<br>${format(t.km)} km｜${format(t.fuel)} L｜${format(t.efficiency)} L/100km`;
      let hasTrack = false;
      for (const segment of t.segments) {
        if (segment.length >= 2) {
          leaflet.polyline(segment, {color:colorOf(t),weight:shown.length === 1 ? 5 : 3,opacity:shown.length === 1 ? .9 : .7}).addTo(layer).bindPopup(popup);
          bounds.push(...segment); hasTrack = true;
        }
      }
      if (hasTrack) tracks++;
      marker(t.origin,'#16803c','起點',`起點<br>${popup}`);
      marker(t.destination,'#c92a2a','終點',`終點<br>${popup}`);
      bounds.push(t.origin,t.destination);
      for (const [i,s] of t.stops.entries()) {
        stops++;
        marker([s.lat,s.lon],'#d97706',`疑似停靠 ${s.minutes} 分鐘`, `疑似停靠點 ${i+1}<br>${escape(t.vehicle)} / ${escape(t.journey)}<br>${escape(s.start)}<br>至 ${escape(s.end)}<br><b>${s.minutes} 分鐘</b>`);
      }
    }
    if (query) bounds.push(query.origin,query.destination);
    if (bounds.length) {
      const wide = globalThis.innerWidth > 1200;
      map.fitBounds(bounds,{paddingTopLeft:[wide && !$('searchPanel').hidden ? 410 : 30,30],paddingBottomRight:[wide ? 380 : 30,30],maxZoom:15});
    }
    $('drawStatus').textContent = `地圖顯示 ${shown.length} 趟，${tracks} 趟有 GPS 軌跡；疑似中途停靠 ${stops} 處。${shown.length > tracks ? '其餘僅標示起終點。' : ''}`;
  }
  function resultButton(label, rows) {
    const button = doc.createElement('button'); button.type = 'button'; button.textContent = label;
    button.addEventListener('click', () => {selectTrips(rows); if (globalThis.innerWidth <= 760 && !$('searchPanel').hidden) toggleSearch();});
    return button;
  }
  function table(headers) {
    const table = doc.createElement('table'), head = doc.createElement('thead'), row = doc.createElement('tr'), body = doc.createElement('tbody');
    for (const text of headers) {const th = doc.createElement('th'); th.textContent = text; row.append(th);}
    head.append(row); table.append(head,body); return {table,body};
  }
  function cell(row, value) {
    const td = doc.createElement('td'); if (typeof value === 'string') td.textContent = value; else td.append(value); row.append(td);
  }
  function renderResults() {
    const groups = summaries(matches);
    const grouped = groups.filter(g => g.group);
    const ungrouped = matches.filter(t => !t.group).length;
    $('resultCounts').textContent = `${matches.length} 趟符合｜${new Set(matches.map(t=>t.vehicle)).size} 輛車｜${grouped.length} 個既有群組｜${ungrouped} 趟未分群`;
    $('queryDetails').textContent = query ? `${query.origin.join(', ')} → ${query.destination.join(', ')}；兩端各 ${query.radius.toLocaleString()} m。` : '';
    $('groupResults').replaceChildren(); $('tripResults').replaceChildren();
    if (matches.length) {
      const {table:groupTable,body} = table(['所屬群組','符合趟數','耗油中位數']);
      for (const g of groups) {
        const row = doc.createElement('tr');
        const button = resultButton(g.group || '未分群', g.rows);
        const dot = doc.createElement('i'); dot.className = 'group-color'; dot.style.background = g.group ? colors.get(g.group) : '#667085'; button.prepend(dot);
        const total = data.groups[g.group]?.count;
        cell(row,button); cell(row,`${g.count}${total ? ` / ${total}` : ''}`); cell(row,`${format(g.medianFuel)} L`); body.append(row);
      }
      const scroll = doc.createElement('div'); scroll.className = 'table-scroll'; scroll.append(groupTable); $('groupResults').append(scroll);
      const note = doc.createElement('p'); note.className = 'muted'; note.textContent = '趟數為符合搜尋／群組總趟數；耗油中位數僅計算本次符合行程。未分群行程合列，不視為同一路徑。'; $('groupResults').append(note);
      const {table:tripTable,body:tripBody} = table(['行程','群組','起點距離','終點距離','里程','L/100km','品質']);
      for (const t of matches) {
        const row = doc.createElement('tr');
        cell(row,resultButton(`${t.vehicle} / ${t.journey}`, [t])); cell(row,t.group || '未分群');
        cell(row,`${format(t.originGap)} m`); cell(row,`${format(t.destinationGap)} m`);
        cell(row,`${format(t.km)} km`); cell(row,format(t.efficiency)); cell(row,t.screened ? '通過' : '未通過'); tripBody.append(row);
      }
      $('tripResults').append(tripTable);
    }
    $('download').disabled = !matches.length; $('showAll').disabled = !matches.length;
  }
  $('searchForm').addEventListener('submit', event => {
    event.preventDefault();
    try {
      if (fields.some(id => !$(id).value.trim()) || !$('radius').value.trim()) throw Error('請填寫完整起終點座標與容許距離。');
      const values = fields.map(id => Number($(id).value));
      const next = {origin:values.slice(0,2),destination:values.slice(2),radius:Number($('radius').value),screenedOnly:$('screenedOnly').checked};
      const found = find(data.trips,next); query = next; matches = found;
      selected.clear(); matches.forEach(t => selected.add(key(t)));
      queryLayer.clearLayers();
      for (const [point,color,label] of [[query.origin,'#16803c','輸入起點'],[query.destination,'#c92a2a','輸入終點']])
        leaflet.circle(point,{radius:query.radius,color,weight:2,fillOpacity:.06}).bindTooltip(`${label}：${query.radius} 公尺`).addTo(queryLayer);
      renderChecks(); renderResults(); draw();
      status(`${query.origin.join(', ')} → ${query.destination.join(', ')}；兩端各 ${query.radius.toLocaleString()} m。${matches.length ? `找到 ${matches.length} 趟。` : '沒有符合行程，可放寬距離或交換方向。'}`);
      if (globalThis.innerWidth <= 760 && !$('searchPanel').hidden) toggleSearch();
    } catch (error) {status(error.message,true);}
  });
  $('fillSeed').addEventListener('click', () => {fillSeed(); status('已帶入所選行程座標；請按搜尋更新結果。');});
  $('swapEndpoints').addEventListener('click', () => {
    const values = fields.map(id => $(id).value); fields.forEach((id,i) => {$(id).value = values[(i+2)%4];}); status('起終點已交換；請按搜尋更新結果。');
  });
  $('searchForm').addEventListener('input', () => status('條件已修改，請按搜尋更新；地圖仍保留上次結果。'));
  $('toggleSearch').addEventListener('click',toggleSearch);
  $('showAll').addEventListener('click',() => selectTrips(matches));
  $('clearSearch').addEventListener('click',() => {
    query = null; matches = []; selected.clear(); layer.clearLayers(); queryLayer.clearLayers(); renderChecks(); renderResults();
    $('drawStatus').textContent = ''; status('已清除結果。輸入座標後可再次搜尋。'); map.setView([23.7,120.9],7);
  });
  $('download').addEventListener('click', () => {
    if (!matches.length) return;
    const url = URL.createObjectURL(new Blob([csv(matches)],{type:'text/csv;charset=utf-8'}));
    const a = doc.createElement('a'); a.href = url; a.download = 'common_od_trips.csv'; doc.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url),1000);
  });
  const resize = () => {
    doc.documentElement.style.setProperty('--header-height',`${doc.querySelector('header').getBoundingClientRect().height}px`);
    map.invalidateSize();
  };
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(resize).observe(doc.querySelector('header'));
  resize(); $('showAll').disabled = true;
  return {map, get matches(){return matches;}, selected};
}
if (typeof document !== 'undefined' && typeof dataset !== 'undefined') mountODMap(dataset);
if (typeof module !== 'undefined' && module.exports) module.exports.mount = mountODMap;
