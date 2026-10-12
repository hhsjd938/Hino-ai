// All detection is done by Python. This module only schedules observations and speech.
export class ReplayClock {
  constructor(data) { this.data = data; this.speed = 1; this.reset(); }
  reset() { this.position = 0; this.index = -1; this.eventIndex = 0; this.running = false; this.finished = false; this.lastWall = 0; }
  start(now) { if (!this.finished) { this.running = true; this.lastWall = now; } }
  advance(now) {
    if (!this.running && this.index < 0) { this.lastWall = now; return {frames: [], events: [], frame: this.data.frames[0], done: false}; }
    if (this.running) this.position = Math.min(this.data.duration, this.position + Math.max(0, now - this.lastWall) / 1000 * this.speed);
    this.lastWall = now;
    const frames = [], events = [];
    while (this.index + 1 < this.data.frames.length && this.data.frames[this.index + 1].at <= this.position) frames.push(this.data.frames[++this.index]);
    while (this.eventIndex < this.data.events.length && this.data.events[this.eventIndex].at <= this.position) events.push(this.data.events[this.eventIndex++]);
    if (this.position >= this.data.duration) { this.running = false; this.finished = true; }
    return { frames, events, frame: this.data.frames[Math.max(0, this.index)], done: this.finished };
  }
  pause(now) { const batch = this.advance(now); this.running = false; return batch; }
  setSpeed(speed, now) { const batch = this.advance(now); this.speed = speed; return batch; }
}

export class SpeechQueue {
  constructor({synth, makeUtterance, now, status}) {
    this.synth = synth; this.makeUtterance = makeUtterance; this.now = now; this.status = status;
    this.enabled = true; this.voice = null; this.queue = []; this.cooldown = new Map();
    this.current = null; this.lastStart = -Infinity; this.generation = 0;
  }
  offer(events) {
    const hasCombined = events.some(e => e.code === 'high_rpm_low_speed' && e.phase === 'start');
    for (const event of events) {
      if (!this.enabled || !event.message || event.phase !== 'start' || (hasCombined && event.code === 'high_rpm')) continue;
      if (event.at - (this.cooldown.get(event.code) ?? -Infinity) < 30) continue;
      if (this.current?.code === event.code || this.queue.some(item => item.code === event.code)) continue;
      this.queue.push({...event, expires: this.now() + 10000});
      this.queue.sort((a,b) => a.priority - b.priority || a.at - b.at);
      this.queue = this.queue.slice(0, 3);
    }
  }
  pump(force = false) {
    if (this.current && this.now() - this.lastStart > 20000) { this.cancel(); this.status('語音 API 回應逾時；文字提醒繼續'); }
    this.queue = this.queue.filter(item => item.expires > this.now());
    if (!this.enabled || this.current || !this.queue.length || (!force && this.now() - this.lastStart < 8000)) return;
    if (!this.synth || !this.makeUtterance) { this.queue = []; this.status('此瀏覽器不支援語音，文字提醒仍可使用'); return; }
    if (!this.voice) { this.queue = []; this.status('尚無可用中文語音，請選擇聲音或使用文字提醒'); return; }
    const event = this.queue.shift();
    const utterance = this.makeUtterance(event.message);
    utterance.lang = this.voice.lang || 'zh-TW'; utterance.voice = this.voice; utterance.rate = 1; utterance.pitch = 1;
    const generation = this.generation;
    this.current = event; this.currentUtterance = utterance; this.lastStart = this.now();
    this.cooldown.set(event.code, event.at);
    utterance.onend = () => { if (generation === this.generation) { this.current = null; this.currentUtterance = null; this.status('語音 API 已完成播報（請確認實際聲音）'); } };
    utterance.onerror = e => { if (generation === this.generation) { this.current = null; this.currentUtterance = null; this.status(`語音未完成：${e.error || '未知錯誤'}；文字提醒繼續`); } };
    try { this.synth.resume(); this.synth.speak(utterance); this.status(`語音 API 已送出：${event.name}`); }
    catch (error) { this.current = null; this.currentUtterance = null; this.status(`無法播報：${error.message}`); }
  }
  cancel(reset = false) {
    this.generation++; this.queue = []; this.current = null; this.currentUtterance = null;
    this.synth?.cancel();
    if (reset) { this.cooldown.clear(); this.lastStart = -Infinity; }
  }
  test() {
    this.cancel(true);
    this.offer([{code: 'test', name: '測試語音', at: 0, phase: 'start', priority: -1, message: 'HINO EcoDrive 中文語音測試，請確認您可以聽到這段提醒。'}]);
    this.pump(true);
  }
}

export default function render({data, parentElement}) {
  const $ = selector => parentElement.querySelector(selector);
  const clock = new ReplayClock(data);
  const listeners = [];
  const listen = (el, event, handler) => { el.addEventListener(event, handler); listeners.push(() => el.removeEventListener(event, handler)); };
  const format = (v, digits = 1) => v == null ? '—' : Number(v).toFixed(digits);
  const duration = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2,'0')}`;
  const counts = new Map();
  const status = text => { $('#speech-status').textContent = text; };
  const speech = new SpeechQueue({synth: globalThis.speechSynthesis,
    makeUtterance: globalThis.SpeechSynthesisUtterance ? text => new SpeechSynthesisUtterance(text) : null,
    now: () => performance.now(), status});
  let voices = [], latest = null, ended = false, lastAlert = null;

  $('#scenario-title').textContent = data.title;
  $('#progress').max = data.duration || 1;
  $('#rule-info').textContent = `急加速：V4−V3 >${data.config.accel_one}，V4−V2、V3−V1 >${data.config.accel_two} km/h；急減速兩窗 ≤${data.config.decel_two} km/h。怠速 ${data.config.idle_seconds} 秒；高 RPM >${data.config.high_rpm}／低速 <${data.config.low_speed} km/h。`;

  function refreshVoices() {
    const previous = $('#voice').value;
    voices = speech.synth?.getVoices().filter(v => /^zh|^cmn/i.test(v.lang)) || [];
    voices.sort((a,b) => Number(/TW|Hant/i.test(b.lang)) - Number(/TW|Hant/i.test(a.lang)));
    $('#voice').replaceChildren();
    if (!voices.length) {
      const option = document.createElement('option'); option.textContent = '未找到中文語音'; $('#voice').append(option);
      speech.voice = null; status(speech.synth ? '系統未提供中文語音；仍可顯示文字警告' : '瀏覽器不支援語音；仍可顯示文字警告');
    } else {
      voices.forEach(v => { const option = document.createElement('option'); option.value = v.voiceURI; option.textContent = `${v.name} (${v.lang})`; $('#voice').append(option); });
      if (voices.some(v => v.voiceURI === previous)) $('#voice').value = previous;
      speech.voice = voices.find(v => v.voiceURI === $('#voice').value) || voices[0];
      status('中文語音已就緒，請按「測試語音」確認實際聲音');
    }
  }
  refreshVoices();
  if (speech.synth) listen(speech.synth, 'voiceschanged', refreshVoices);
  listen($('#voice'), 'change', () => { speech.cancel(); speech.voice = voices.find(v => v.voiceURI === $('#voice').value); });
  listen($('#speech-enabled'), 'change', e => { speech.enabled = e.target.checked; if (!speech.enabled) { speech.cancel(); status('語音已關閉'); } });
  listen($('#test-speech'), 'click', () => { if (!speech.enabled) { status('請先啟用語音'); return; } speech.test(); });

  function showFrame(frame) {
    latest = frame;
    $('#speed').textContent = format(frame.speed, 0); $('#rpm').textContent = format(frame.rpm, 0);
    $('#acceleration').textContent = format(frame.acceleration, 2); $('#idle').textContent = format(frame.idle_seconds, 0);
    $('#data-time').textContent = frame.time;
    $('#progress').value = clock.position; $('#elapsed').textContent = `${duration(clock.position)} / ${duration(data.duration)}`;
    $('#position').textContent = `${Math.max(1, clock.index + 1)} / ${data.frames.length}`;
    const states = {0:'停車',1:'行駛',2:'怠速'};
    $('#car-state').textContent = `${states[frame.car_status] || '未知'} · 限速 ${format(frame.speed_limit,0)} km/h`;
    $('#data-quality').textContent = !frame.signal_valid ? '訊號缺值或 CAN 異常；持續狀態已中斷' : frame.gap ? '資料時間缺口；持續狀態已重新計算' : '依原始時間播放；怠速與高 RPM 時間為觀測估計';
    $('#data-quality').className = (!frame.signal_valid || frame.gap) ? 'quality warn' : 'quality';
    let code = frame.active[0];
    if (lastAlert && frame.at - lastAlert.at <= 5 && (!code || lastAlert.priority < (data.events.find(e => e.code === code)?.priority ?? 99))) code = lastAlert.code;
    $('#alert').className = code ? 'alert warning' : 'alert';
    $('#alert-title').textContent = code ? `提醒 · ${data.names[code]}` : '目前無駕駛提醒';
    $('#alert-message').textContent = code ? data.messages[code] : '保持平順操作，專心注意道路狀況。';
    $('#alert-source').textContent = code ? (code.startsWith('high_') ? '自訂規則' : data.real ? '車機回報' : '模擬規則') : 'ECO COACH';
    $('#distance').textContent = `${format(frame.distance_km,2)} km`;
    $('#fuel').textContent = data.real ? `${format(frame.fuel_l,2)} L` : '模擬情境不估算耗油';
  }

  function addEvents(events) {
    for (const event of events) {
      if (event.phase === 'start') {
        counts.set(event.code, (counts.get(event.code) || 0) + 1);
        if (event.message && (!lastAlert || event.at > lastAlert.at || event.priority < lastAlert.priority)) lastAlert = event;
      }
      const tr = document.createElement('tr');
      const values = [event.time, event.name + (event.phase === 'end' ? ' · 解除' : ''), event.source, event.original_time || '—', event.message || (event.phase === 'end' ? '事件已解除' : '原始事件紀錄')];
      for (const value of values) { const td = document.createElement('td'); td.textContent = value; tr.append(td); }
      $('#event-rows').prepend(tr);
    }
    $('#event-count').textContent = Array.from(counts.values()).reduce((a,b) => a+b, 0);
    $('#event-empty').hidden = $('#event-rows').children.length > 0;
    // Suppress the generic high-RPM voice while the more specific condition is active.
    speech.offer(events.filter(e => !(e.code === 'high_rpm' && latest?.active.includes('high_rpm_low_speed'))));
  }

  function showSummary() {
    $('#summary').hidden = false;
    const list = $('#summary-counts'); list.replaceChildren();
    for (const [code,count] of counts) { const item = document.createElement('span'); item.className = 'chip'; item.textContent = `${data.names[code] || code} ${count} 次`; list.append(item); }
    if (!counts.size) list.textContent = '本次播放沒有觸發事件。';
    const suggestions = [];
    if (counts.has('7')) suggestions.push('急減速較多時，可留意預判路況與安全車距。');
    if (counts.has('6')) suggestions.push('加速時平順操作，避免不必要的急踩油門。');
    if (counts.has('8')) suggestions.push('依道路速限行駛，提前規劃減速。');
    if (counts.has('2')) suggestions.push('視停靠作業與安全需求，減少不必要的長時間怠速。');
    if (counts.has('high_rpm') || counts.has('high_rpm_low_speed')) suggestions.push('留意轉速與檔位，減少不必要的高轉速操作。');
    $('#coaching').textContent = suggestions.join(' ') || '繼續保持平順駕駛。';
    const reference = data.metadata.reference;
    if (data.real && reference) {
      const deviation = latest?.fuel_l != null && reference.median_fuel_l > 0 ? (latest.fuel_l / reference.median_fuel_l - 1) * 100 : null;
      const rate = latest?.distance_km > 0 && latest?.fuel_l != null ? latest.fuel_l / latest.distance_km * 100 : null;
      $('#reference').textContent = `同路線 ${reference.route_group}（${reference.trip_count} 趟，含本趟）：耗油中位數 ${format(reference.median_fuel_l,2)} L；本趟偏差 ${format(deviation)}%。本趟 ${format(rate,2)} L/100km，群組中位數 ${format(reference.median_l_per_100km,2)} L/100km。關聯比較不代表因果省油效益。`;
    } else $('#reference').textContent = data.real ? '此行程沒有既有同路線群組參考。' : '此為模擬情境，不推算耗油或省油效益。';
  }

  function consume(batch) {
    showFrame(batch.frame); addEvents(batch.events);
    $('#play').textContent = clock.running ? '播放中' : clock.finished ? '重新播放' : '開始 / 繼續';
    $('#play').disabled = clock.running; $('#pause').disabled = !clock.running;
    if (batch.done && !ended) { ended = true; $('#play-status').textContent = '播放完成'; showSummary(); }
  }
  function reset() {
    clock.reset(); speech.cancel(true); counts.clear(); ended = false; lastAlert = null;
    $('#event-rows').replaceChildren(); $('#event-empty').hidden = false; $('#event-count').textContent = '0'; $('#summary').hidden = true;
    showFrame(data.frames[0]); $('#play').disabled = false; $('#pause').disabled = true;
    $('#play').textContent = '開始 / 繼續'; $('#play-status').textContent = '等待開始'; status('已重設；可測試語音或開始回放');
  }
  listen($('#play'), 'click', () => { if (clock.finished) reset(); clock.start(performance.now()); $('#play-status').textContent = '正在回放'; consume(clock.advance(performance.now())); });
  listen($('#pause'), 'click', () => { consume(clock.pause(performance.now())); speech.cancel(); $('#play-status').textContent = '已暫停'; });
  listen($('#restart'), 'click', reset);
  listen($('#playback-speed'), 'change', e => consume(clock.setSpeed(Number(e.target.value), performance.now())));
  listen($('#clear-log'), 'click', () => { $('#event-rows').replaceChildren(); $('#event-empty').hidden = false; });
  listen(document, 'visibilitychange', () => {
    if (document.hidden) { if (clock.running) consume(clock.pause(performance.now())); speech.cancel(); $('#play-status').textContent = '背景分頁已自動暫停'; }
  });
  showFrame(data.frames[0]);
  const timer = setInterval(() => { if (clock.running) consume(clock.advance(performance.now())); speech.pump(); }, 50);
  return () => { clearInterval(timer); clock.running = false; speech.cancel(true); listeners.forEach(remove => remove()); };
}
