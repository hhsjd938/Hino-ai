import test from 'node:test';
import assert from 'node:assert/strict';
import render, {ReplayClock, SpeechQueue} from '../frontend.mjs';

const timeline = {duration:12,frames:[0,1,6,12].map(at=>({at})),events:[0,3,6,12].map((at,id)=>({at,id}))};
test('all playback speeds deliver each irregular observation and event exactly once',()=>{
  for (const speed of [1,10,30,60]) {
    const clock=new ReplayClock(timeline); clock.speed=speed; clock.start(0);
    const frames=[],events=[];
    for (let now=0;!clock.finished;now+=50) {const batch=clock.advance(now);frames.push(...batch.frames);events.push(...batch.events);}
    assert.deepEqual(frames.map(x=>x.at),[0,1,6,12]);assert.deepEqual(events.map(x=>x.id),[0,1,2,3]);
    assert.equal(clock.advance(99999).events.length,0);
  }
});
test('pause freezes data time, resume preserves remaining interval, speed changes do not skip events',()=>{
  const clock=new ReplayClock(timeline);clock.start(0);clock.advance(1000);clock.pause(1500);
  clock.advance(100000);assert.equal(clock.position,1.5);clock.start(100000);
  const batch=clock.setSpeed(10,100500);assert.equal(clock.position,2);assert.equal(batch.events.length,0);
  assert.deepEqual(clock.advance(101000).events.map(x=>x.at),[3,6]);
  clock.reset();assert.equal(clock.index,-1);assert.equal(clock.eventIndex,0);assert.equal(clock.running,false);
});
test('changing speed before start does not deliver the first event',()=>{
  const clock=new ReplayClock(timeline);assert.equal(clock.setSpeed(60,0).events.length,0);
  clock.start(0);assert.equal(clock.advance(0).events.length,1);
});

function fixture() {
  let now=0;const spoken=[],statuses=[];let cancelled=0;
  const queue=new SpeechQueue({synth:{resume(){},speak(u){spoken.push(u)},cancel(){cancelled++}},
    makeUtterance: text=>({text}),now:()=>now,status:text=>statuses.push(text)});
  queue.voice={lang:'zh-TW'};
  return {queue,spoken,statuses,advance:ms=>{now+=ms},cancelled:()=>cancelled};
}
const event=(code,priority,at=0)=>({code,name:code,priority,at,phase:'start',message:code});
test('voice queue uses priority, caps at three, deduplicates, and suppresses generic high RPM',()=>{
  const {queue,spoken}=fixture();
  queue.offer([event('2',5),event('high_rpm',4),event('high_rpm_low_speed',3),event('6',2),event('8',1),event('7',0),event('7',0)]);
  assert.deepEqual(queue.queue.map(e=>e.code),['7','8','6']);queue.pump();assert.equal(spoken[0].text,'7');
  queue.pump();assert.equal(spoken.length,1);
});
test('voice cooldown uses data seconds and start spacing uses wall seconds',()=>{
  const {queue,spoken,advance}=fixture();queue.offer([event('6',2)]);queue.pump();spoken[0].onend();
  queue.offer([event('6',2,29),event('7',0,29)]);assert.equal(queue.queue.length,1);
  advance(7999);queue.pump();assert.equal(spoken.length,1);advance(1);queue.pump();assert.equal(spoken.length,2);
  spoken[1].onend();queue.offer([event('6',2,30)]);advance(8000);queue.pump();assert.equal(spoken.length,3);
});
test('cancel clears pending speech and stale callbacks cannot modify a new session',()=>{
  const {queue,spoken}=fixture();queue.offer([event('6',2)]);queue.pump();const stale=spoken[0];queue.cancel(true);
  queue.offer([event('7',0)]);queue.pump();stale.onend();assert.equal(queue.current.code,'7');
  assert.equal(queue.cooldown.has('6'),false);
});
test('missing voices or API degrade to text; failed utterances unblock the queue',()=>{
  const {queue,spoken,statuses}=fixture();queue.voice=null;queue.offer([event('6',2)]);queue.pump();
  assert.equal(spoken.length,0);assert.match(statuses.at(-1),/中文語音/);
  queue.voice={lang:'zh-TW'};queue.offer([event('7',0)]);queue.pump();spoken[0].onerror({error:'not-allowed'});
  assert.equal(queue.current,null);assert.match(statuses.at(-1),/not-allowed/);
  queue.synth=null;queue.offer([event('6',2,30)]);queue.pump(true);assert.match(statuses.at(-1),/不支援/);
});
test('stale queued voices expire and a stalled API is cancelled after twenty wall seconds',()=>{
  const {queue,spoken,advance,statuses}=fixture();queue.offer([event('6',2)]);advance(10001);queue.pump();assert.equal(spoken.length,0);
  queue.offer([event('7',0)]);queue.pump();advance(20001);queue.pump();assert.equal(queue.current,null);
  assert.match(statuses.at(-1),/逾時/);
});

test('mounted dashboard updates controls, timeline and summary; background pause and cleanup release resources',()=>{
  // A tiny DOM contract fixture, not a browser/audio or visual-layout assertion.
  class Element {
    constructor() {this.children=[];this.handlers=new Map();this.value='';this.textContent='';}
    addEventListener(name,fn){if(!this.handlers.has(name))this.handlers.set(name,new Set());this.handlers.get(name).add(fn);}
    removeEventListener(name,fn){this.handlers.get(name)?.delete(fn);}
    fire(name){for(const fn of this.handlers.get(name)||[])fn({target:this});}
    replaceChildren(...items){this.children=items;}
    append(item){this.children.push(item);}
    prepend(item){this.children.unshift(item);}
  }
  const elements=new Map();
  const root={querySelector(name){if(!elements.has(name))elements.set(name,new Element());return elements.get(name);}};
  const $=name=>root.querySelector('#'+name);
  const doc=new Element();doc.createElement=()=>new Element();doc.hidden=false;
  const synth=new Element();synth.getVoices=()=>[{voiceURI:'tw',name:'Test Chinese',lang:'zh-TW'}];
  synth.cancel=()=>{};synth.resume=()=>{};synth.speak=()=>{};
  const keys=['document','speechSynthesis','SpeechSynthesisUtterance','performance','setInterval','clearInterval'];
  const descriptors=keys.map(key=>Object.getOwnPropertyDescriptor(globalThis,key));
  let now=0,tick=null,cleared=false;
  const overrides=[doc,synth,class{constructor(text){this.text=text;}},{now:()=>now},fn=>{tick=fn;return 123;},id=>{assert.equal(id,123);cleared=true;}];
  keys.forEach((key,i)=>Object.defineProperty(globalThis,key,{value:overrides[i],configurable:true,writable:true}));
  try {
    const frames=[0,1,6,12].map(at=>({at,time:`time-${at}`,speed:30,rpm:1500,car_status:1,speed_limit:80,acceleration:0,
      idle_seconds:0,gap:false,signal_valid:true,distance_km:at,fuel_l:null,active:[]}));
    const data={frames,events:[{...event('6',2,3),id:0,time:'received-3',source:'模擬規則'}],duration:12,real:false,gaps:0,
      config:{accel_one:10,accel_two:5,decel_two:-15,idle_seconds:300,high_rpm:2500,low_speed:30},
      metadata:{vehicle:'SIMULATED',journey:'test'},title:'Test',names:{'6':'急加速'},messages:{'6':'請平順加速'}};
    const cleanup=render({data,parentElement:root});
    assert.equal($('progress').max,12);assert.equal($('scenario-title').textContent,'Test');
    $('play').fire('click');now=1000;tick();assert.equal($('data-time').textContent,'time-1');
    $('pause').fire('click');now=5000;tick();assert.equal($('progress').value,1);
    $('play').fire('click');now=7000;tick();assert.equal($('event-rows').children.length,1);
    assert.equal($('event-count').textContent,1);
    doc.hidden=true;doc.fire('visibilitychange');now=12000;tick();assert.equal($('progress').value,3);
    doc.hidden=false;$('play').fire('click');now=22000;tick();assert.equal($('summary').hidden,false);
    assert.match($('reference').textContent,/模擬情境/);
    $('restart').fire('click');assert.equal($('event-rows').children.length,0);assert.equal($('event-count').textContent,'0');
    cleanup();assert.equal(cleared,true);
    assert.equal([...doc.handlers.values()].reduce((n,set)=>n+set.size,0),0);
  } finally {
    keys.forEach((key,i)=>descriptors[i]?Object.defineProperty(globalThis,key,descriptors[i]):delete globalThis[key]);
  }
});
