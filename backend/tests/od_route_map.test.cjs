const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const od = require('../scripts/data_pipeline/od_route_map.js');
const query = {origin:[23,120],destination:[24,121],radius:1000,screenedOnly:false};
const trip = (vehicle,journey,extra={}) => ({vehicle,journey,origin:[23,120],destination:[24,121],group:'',start:'2025-01-01T08:00:00',
  screened:false,km:5,fuel:1,efficiency:20,segments:[],stops:[],...extra});

test('both endpoints must pass; opposite direction is excluded',()=>{
  const rows = [trip('A','1'),trip('A','2',{destination:[25,121]}),trip('A','3',{origin:[22,120]}),trip('A','4',{origin:query.destination,destination:query.origin})];
  assert.deepEqual(od.find(rows,query).map(t=>t.journey),['1']);
});
test('search crosses vehicles and groups and includes short ungrouped trips by default',()=>{
  const rows = [trip('A','1',{group:'A-R001',screened:true}),trip('B','1',{group:'B-R001'}),trip('C','1')];
  assert.equal(od.find(rows,query).length,3);
  assert.equal(od.find(rows,{...query,screenedOnly:true}).length,1);
  assert.equal(new Set(rows.map(od.key)).size,3);
});
test('radius boundary is inclusive and distances are metres',()=>{
  const row = trip('A','1',{origin:[23.001,120]}); const distance = od.distance(query.origin,row.origin);
  assert.ok(distance>110 && distance<112);
  assert.equal(od.find([row],{...query,radius:distance}).length,1);
  assert.equal(od.find([row],{...query,radius:distance-.001}).length,0);
  for (const radius of [0,NaN,Infinity,100001]) assert.throws(()=>od.find([row],{...query,radius}));
  assert.throws(()=>od.find([row],{...query,origin:[91,120]}));
});
test('group summaries use matched trips and do not call ungrouped trips different routes',()=>{
  const result=od.summaries([trip('A','1',{group:'G',fuel:2}),trip('A','2',{group:'G',fuel:4}),trip('B','1'),trip('C','1')]);
  assert.deepEqual(result.map(g=>[g.group,g.count,g.medianFuel]),[['G',2,3],['',2,1]]);
});
test('CSV preserves ids, quotes cells and guards spreadsheet formulas',()=>{
  const row={...trip('=bad','001',{group:'G,"1"'}),originGap:0,destinationGap:1};
  const csv=od.csv([row]); assert.ok(csv.startsWith('\ufeff'));assert.ok(csv.includes('"001"'));
  assert.ok(csv.includes('"\'=bad"'));assert.ok(csv.includes('"G,""1"""'));
});
test('generated real dataset has all records, OSM and the expected example group',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../../data/processed/analysis/od_route_map.html'),'utf8');
  const data=JSON.parse(html.match(/const dataset=(.*?);\r?\n/)[1]);
  assert.equal(data.total,3541);assert.equal(data.records,493744);assert.equal(data.trips.length,3538);
  assert.ok(html.includes('https://tile.openstreetmap.org/{z}/{x}/{y}.png'));
  const seed=data.trips.find(t=>t.vehicle==='AHMPUL0C13' && t.journey==='250111000001');
  assert.ok(seed);
  const found=od.find(data.trips,{origin:seed.origin,destination:seed.destination,radius:1000,screenedOnly:true});
  assert.equal(found.filter(t=>t.group==='AHMPUL0C13-R001').length,36);
  assert.ok(data.trips.some(t=>t.km<10 && !t.group));
});

// DOM/Leaflet contract fixture. This exercises controls, not a visual/browser test.
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.style={setProperty(){}};this.events={};this.value='';this.checked=false;this.hidden=false;this.textContent='';this.classes=new Set();this.classList={add:c=>this.classes.add(c),toggle:(c,on)=>on?this.classes.add(c):this.classes.delete(c)};}
  get value(){return this._value;}
  set value(value){this._value=String(value);}
  addEventListener(event,fn){(this.events[event] ||= []).push(fn);}
  fire(event){for(const fn of this.events[event]||[]) fn({preventDefault(){},target:this});}
  append(...children){this.children.push(...children);}
  prepend(child){this.children.unshift(child);}
  replaceChildren(...children){this.children=children;}
  add(option){this.children.push(option);if(this.children.length===1)this.value=option.value;}
  setAttribute(name,value){this[name]=value;}
  getBoundingClientRect(){return {height:68};}
  querySelectorAll(selector){const result=[];const attr=selector.includes('data-trip')?'trip':'all';const visit=e=>{if(e.tag==='input' && attr in e.dataset)result.push(e);for(const child of e.children||[])visit(child);};visit(this);return result;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
test('mounted map searches, filters groups, toggles trips, swaps direction and clears without changing original data',()=>{
  const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const doc={getElementById:get,createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),body:new Element(),documentElement:new Element(),querySelector:()=>new Element()};
  get('radius').value='1000';
  const calls={tiles:[],lines:[],markers:[]};const item=()=>({addTo(){return this;},bindTooltip(){return this;},bindPopup(){return this;},on(){return this;}});
  const map={setView(){return this;},fitBounds(){return this;},invalidateSize(){}};
  const leaflet={map:()=>map,control:{zoom:item},tileLayer:(url)=>{calls.tiles.push(url);return item();},layerGroup:()=>({...item(),clearLayers(){}}),
    polyline:points=>{calls.lines.push(points);return item();},circleMarker:point=>{calls.markers.push(point);return item();},circle:item};
  const rows=[trip('A','1',{group:'G',screened:true,segments:[[[23,120],[23.1,120.1]],[[23.9,120.9],[24,121]]]}),trip('B','1')];
  const data={trips:rows,groups:{G:{count:4}},total:2,invalid:0};
  const context={module:{exports:{}},console,Option:class{constructor(text,value){this.text=text;this.value=value;}},innerWidth:1400};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../scripts/data_pipeline/od_route_map.js'),'utf8'),context);
  const mounted=context.module.exports.mount(data,doc,leaflet);
  assert.equal(calls.tiles[0],'https://tile.openstreetmap.org/{z}/{x}/{y}.png');
  get('searchForm').fire('submit');assert.equal(mounted.matches.length,2);assert.equal(mounted.selected.size,2);
  assert.equal(calls.lines.length,2); // Two observed segments, never an invented connection.
  assert.match(get('resultCounts').textContent,/1 個既有群組/);
  const check=get('tripOptions').querySelectorAll('input[data-trip]')[0];check.checked=false;check.fire('change');assert.equal(mounted.selected.size,1);
  get('showAll').fire('click');assert.equal(mounted.selected.size,2);
  const groupTable=get('groupResults').children[0].children[0];
  const groupButton=groupTable.children[1].children[0].children[0].children[0];groupButton.fire('click');assert.equal(mounted.selected.size,1);
  get('swapEndpoints').fire('click');get('searchForm').fire('submit');assert.equal(mounted.matches.length,0);
  get('swapEndpoints').fire('click');get('screenedOnly').checked=true;get('searchForm').fire('submit');assert.equal(mounted.matches.length,1);
  get('clearSearch').fire('click');assert.equal(mounted.matches.length,0);assert.equal(get('download').disabled,true);
  assert.equal(data.trips.length,2);assert.equal(data.trips[0].segments.length,2);
});
