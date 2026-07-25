const $ = s => document.querySelector(s);
let hours = 24;
let selectedHomekitDevice = null;
let useHomekit = false;
let history = {samples:[], transitions:[], observations:[]};
let runtimeHistory = {samples:[], transitions:[], observations:[]};
let runtimeDays = 30;
let runtimeLoadedAt = 0;

function fmt(v, digits=0){ return v == null ? "—" : Number(v).toFixed(digits) }
function age(iso){
  if(!iso) return "—";
  const m=Math.max(0,Math.round((Date.now()-new Date(iso))/60000));
  return m<1?"now":m<60?`${m}m ago`:`${Math.round(m/60)}h ago`;
}
function friendlyMode(mode){
  if(!mode) return "Unknown";
  return mode.replace(/([a-z])([A-Z])/g,"$1 $2");
}

async function get(url, options){
  const r=await fetch(url, options);
  const body=await r.json();
  if(!r.ok) throw new Error(body.error||r.statusText);
  return body;
}

async function load(){
  const [status, homekit]=await Promise.all([get("/api/status"), get("/api/homekit/status").catch(e => ({ready:false,last_error:e.message}))]);
  renderHomekit(homekit);
  $("#connectPanel").classList.toggle("hidden",status.connected || homekit.paired);
  $("#dashboard").classList.toggle("hidden",!(status.connected || homekit.paired));
  $("#connection").textContent=status.connected?(status.device?.device_name||"Connected"):homekit.paired?"HomeKit local":"Not connected";
  $("#connection").classList.toggle("online",status.connected || homekit.paired);
  if(!(status.connected || homekit.paired)) return;
  const local = homekitReading(homekit);
  const s=status.latest || local;
  $("#deviceName").textContent=status.device?.device_name || (homekit.paired ? "T10 local" : "T10");
  $("#coolingStages").textContent=status.configuration?.systemConfiguration?.coolingStages??"Not reported";
  if(s){
    const mode=friendlyMode(s.operation_mode);
    $("#operationMode").textContent=mode;
    $("#stateDot").className="state-dot "+(String(s.operation_mode).toLowerCase().includes("cool")?"cooling":"off");
    $("#stateExplanation").textContent=String(s.operation_mode).toLowerCase().includes("cool")
      ?"Thermostat relay status indicates a cooling request. Physical compressor operation is unverified."
      :"No cooling request is reported in the latest API sample.";
    $("#indoorTemp").textContent=fmt(s.indoor_temp,1);
    $("#coolSetpoint").textContent=fmt(s.cool_setpoint,1);
    $("#humidity").textContent=fmt(s.indoor_humidity);
    $("#lastAge").textContent=age(s.captured_at);
  }
  $("#pollHealth").textContent=homekit.paired ? (homekit.last_poll_error?`Read error: ${homekit.last_poll_error}`:"Active HomeKit read every 30 seconds") : status.last_poll?.ok===false?`Error: ${status.last_poll.error}`:`Every ${status.poll_seconds/60} minutes`;
  useHomekit=Boolean(homekit.paired);
  history=await get(useHomekit ? `/api/homekit/history?hours=${hours}` : `/api/history?hours=${hours}`);
  if(!status.connected && homekit.paired){
    if(!history.samples.length && local)history.samples=[local];
    history.observations=(history.observations||[]).map(e=>({...e,kind:e.kind||hkLabel(e.characteristic),note:e.note||hkEventDetail(e)}));if(hoverEvent&&!isGraphEvent(hoverEvent)){hoverEvent=null;hoverTime=null}
  }
  await loadRuntimeData(false);
  detailModule.setData({device: status.device || {device_name: homekit.paired ? "T10 local" : "Thermostat"}, snapshot: s, history: runtimeHistory, source: homekit.paired ? "HomeKit local" : "Resideo cloud"});
  drawChart();
  drawTimeline();
  $("#rawJson").textContent=JSON.stringify(status.connected ? await get("/api/raw/latest") : homekit.current,null,2);
}



const seriesDefaults={temperature:true,setpoint:true,humidity:true,cooling:true,fan:true,events:true};
let seriesVisibility={...seriesDefaults};
try{seriesVisibility={...seriesDefaults,...JSON.parse(localStorage.getItem("thermostat-chart-series")||"{}")}}catch(_){}
function setupSeriesToggles(){document.querySelectorAll("[data-series]").forEach(button=>{const key=button.dataset.series;const sync=()=>{const on=seriesVisibility[key]!==false;button.classList.toggle("off",!on);button.setAttribute("aria-pressed",String(on))};sync();button.onclick=()=>{seriesVisibility[key]=!seriesVisibility[key];try{localStorage.setItem("thermostat-chart-series",JSON.stringify(seriesVisibility))}catch(_){}if(key==="events"&&!seriesVisibility.events){hoverEvent=null;$("#eventInspector").classList.add("hidden")}sync();drawChart()}})}
setupSeriesToggles();
let graphView=null, graphGeometry=null, hoverTime=null, hoverEvent=null, dragState=null;

function graphDomain(){
  const end=Date.now();
  return {start:end-hours*3600000,end};
}
function sampleTime(row){return new Date(row.captured_at).getTime()}
function isGraphEvent(event){return String(event.characteristic||"").slice(0,8).toUpperCase()!=="00000010"&&event.kind!=="Humidity"}
function sortedSamples(){return history.samples.map(r=>({...r,_t:sampleTime(r)})).filter(r=>Number.isFinite(r._t)).sort((a,b)=>a._t-b._t)}
function durationLabel(ms){
  const minutes=Math.round(ms/60000);
  if(minutes<60)return `${minutes} minute${minutes===1?"":"s"}`;
  const hrs=ms/3600000;if(hrs<48)return `${Math.round(hrs*10)/10} hours`;
  return `${Math.round(hrs/24*10)/10} days`;
}
function axisTime(t,span){
  const d=new Date(t);
  if(span>=48*3600000)return d.toLocaleDateString(undefined,{month:"short",day:"numeric"});
  if(span>=12*3600000)return d.toLocaleTimeString(undefined,{hour:"numeric"});
  if(span>=3600000)return d.toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit"});
  return d.toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit",second:span<10*60000?"2-digit":undefined});
}
function updateWindowLabel(t0,t1,live){
  const sameDay=new Date(t0).toDateString()===new Date(t1).toDateString();
  const start=new Date(t0).toLocaleString(undefined,sameDay?{hour:"numeric",minute:"2-digit"}:{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"});
  const end=new Date(t1).toLocaleString(undefined,{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"});
  $("#chartWindow").textContent=`${durationLabel(t1-t0)} view · ${start} – ${end}${live?" · live":""}`;
}

function drawChart(){
  const canvas=$("#chart"),rect=canvas.getBoundingClientRect(),dpr=Math.min(devicePixelRatio||1,2);
  const W=Math.max(260,rect.width),H=Math.max(330,rect.height||380);
  canvas.width=Math.round(W*dpr);canvas.height=Math.round(H*dpr);
  const c=canvas.getContext("2d");c.setTransform(dpr,0,0,dpr,0,0);c.clearRect(0,0,W,H);
  const p={l:50,r:50,t:24,tempB:H-112,coolY:H-84,fanY:H-56,axisY:H-20};
  const full=graphDomain();
  const span=graphView?graphView.end-graphView.start:full.end-full.start;
  let t1=graphView?(graphView.live?full.end:graphView.end):full.end;
  let t0=graphView?t1-span:full.start;
  if(t0<full.start){t0=full.start;t1=Math.min(full.end,t0+span)}
  if(t1>full.end){t1=full.end;t0=Math.max(full.start,t1-span)}
  const live=!graphView||Boolean(graphView.live);
  if(graphView?.live)graphView={start:t0,end:t1,live:true};
  updateWindowLabel(t0,t1,live);
  const x=t=>p.l+(t-t0)/(t1-t0)*(W-p.l-p.r);
  const all=sortedSamples();
  const dataRows=all.filter(r=>r._t>=t0&&r._t<=t1);
  const before=[...all].reverse().find(r=>r._t<t0);
  const after=all.find(r=>r._t>t1);
  const plotRows=[...(before?[before]:[]),...dataRows,...(after?[after]:[])];
  $("#chartEmpty").classList.toggle("hidden",dataRows.length>0);

  const tempKeys=[];
  if(seriesVisibility.temperature)tempKeys.push("indoor_temp");
  if(seriesVisibility.setpoint)tempKeys.push("cool_setpoint");
  const temps=dataRows.flatMap(r=>tempKeys.map(key=>r[key])).filter(v=>v!=null).map(Number).filter(Number.isFinite);
  if(!temps.length&&before)for(const key of tempKeys){const v=before[key];if(v!=null&&Number.isFinite(Number(v)))temps.push(Number(v))}
  let lo=temps.length?Math.floor((Math.min(...temps)-.75)*2)/2:65;
  let hi=temps.length?Math.ceil((Math.max(...temps)+.75)*2)/2:80;
  if(hi-lo<3){const mid=(hi+lo)/2;lo=Math.floor((mid-1.5)*2)/2;hi=lo+3}
  const y=v=>p.t+(hi-v)/(hi-lo)*(p.tempB-p.t);

  const humidities=dataRows.map(r=>r.indoor_humidity).filter(v=>v!=null).map(Number).filter(Number.isFinite);
  if(!humidities.length&&before?.indoor_humidity!=null&&Number.isFinite(Number(before.indoor_humidity)))humidities.push(Number(before.indoor_humidity));
  let humidityLo=humidities.length?Math.max(0,Math.floor(Math.min(...humidities)-2)):40;
  let humidityHi=humidities.length?Math.min(100,Math.ceil(Math.max(...humidities)+2)):60;
  if(humidityHi-humidityLo<10){const mid=(humidityHi+humidityLo)/2;humidityLo=Math.max(0,Math.floor(mid-5));humidityHi=Math.min(100,humidityLo+10);humidityLo=Math.max(0,humidityHi-10)}
  const yHumidity=v=>p.t+(humidityHi-v)/(humidityHi-humidityLo)*(p.tempB-p.t);
  graphGeometry={W,H,p,t0,t1,full,x,y,yHumidity};

  c.font="11px system-ui";c.textBaseline="middle";
  for(let i=0;i<=4;i++){
    const Y=p.tempB-(p.tempB-p.t)*i/4;
    c.strokeStyle="#deddd6";c.lineWidth=1;c.beginPath();c.moveTo(p.l,Y);c.lineTo(W-p.r,Y);c.stroke();
    if(seriesVisibility.temperature||seriesVisibility.setpoint){const v=lo+(hi-lo)*i/4;c.fillStyle="#66736f";c.textAlign="right";c.fillText(`${v.toFixed(1)}°`,p.l-8,Y)}
    if(seriesVisibility.humidity){const v=humidityLo+(humidityHi-humidityLo)*i/4;c.fillStyle="#7656a6";c.textAlign="left";c.fillText(`${v.toFixed(0)}%`,W-p.r+7,Y)}
  }
  const tickCount=W<430?3:5;
  for(let i=0;i<tickCount;i++){
    const t=t0+(t1-t0)*i/(tickCount-1),X=x(t);
    c.strokeStyle="#e8e6df";c.beginPath();c.moveTo(X,p.t);c.lineTo(X,p.tempB);c.stroke();
    c.fillStyle="#66736f";c.textAlign=i===0?"left":i===tickCount-1?"right":"center";c.textBaseline="alphabetic";
    c.fillText(axisTime(t,t1-t0),X,p.axisY);
  }
  c.textAlign="left";c.textBaseline="middle";c.fillStyle="#66736f";c.font="10px system-ui";
  if(seriesVisibility.cooling){c.fillText("COOL",4,p.coolY+7);c.fillStyle="#efeee8";c.fillRect(p.l,p.coolY,W-p.l-p.r,14)}
  if(seriesVisibility.fan){c.fillStyle="#66736f";c.fillText("FAN*",8,p.fanY+7);c.fillStyle="#efeee8";c.fillRect(p.l,p.fanY,W-p.l-p.r,14)}

  c.save();c.beginPath();c.rect(p.l,p.t,W-p.l-p.r,p.tempB-p.t);c.clip();
  function drawSeries(key,color,yMap,{step=false,dashed=false,points=false}={}){
    const source=step?[...(before?[before]:[]),...dataRows]:plotRows;
    const valid=source.filter(r=>r[key]!=null&&Number.isFinite(Number(r[key])));
    if(!valid.length)return;
    c.strokeStyle=color;c.lineWidth=step?2:2.4;c.lineJoin="round";c.lineCap="round";c.setLineDash(dashed?[7,5]:[]);c.beginPath();
    let prev=null;
    for(const row of valid){
      const X=x(row._t),Y=yMap(Number(row[key]));
      if(!prev)c.moveTo(X,Y);
      else if(step){c.lineTo(X,prev.Y);c.lineTo(X,Y)}
      else c.lineTo(X,Y);
      prev={X,Y,t:row._t};
    }
    if(step&&prev)c.lineTo(x(t1),prev.Y);
    c.stroke();c.setLineDash([]);
    if(points&&(t1-t0)<=6*3600000){c.fillStyle=color;for(const row of dataRows){if(row[key]==null||!Number.isFinite(Number(row[key])))continue;c.beginPath();c.arc(x(row._t),yMap(Number(row[key])),1.8,0,Math.PI*2);c.fill()}}
  }
  if(seriesVisibility.setpoint)drawSeries("cool_setpoint","#1c6b58",y,{step:true,dashed:true});
  if(seriesVisibility.temperature)drawSeries("indoor_temp","#e87932",y,{points:true});
  if(seriesVisibility.humidity)drawSeries("indoor_humidity","#7656a6",yHumidity,{points:true});
  if(seriesVisibility.events)for(const o of history.observations.filter(isGraphEvent)){const X=x(new Date(o.occurred_at).getTime());if(X<p.l||X>W-p.r)continue;c.strokeStyle="#bb3d32";c.lineWidth=1;c.beginPath();c.moveTo(X,p.t);c.lineTo(X,p.tempB);c.stroke();c.fillStyle="#bb3d32";c.beginPath();c.moveTo(X-4,p.t);c.lineTo(X+4,p.t);c.lineTo(X,p.t+6);c.closePath();c.fill()}
  c.restore();

  const stateRows=[...(before?[before]:[]),...dataRows];
  for(let i=0;i<stateRows.length;i++){
    const row=stateRows[i],next=stateRows[i+1];
    const a=Math.max(t0,row._t),b=Math.min(t1,next?next._t:t1);if(b<=a)continue;
    if(seriesVisibility.cooling&&String(row.operation_mode).toLowerCase().includes("cool")){c.fillStyle="#347b98";c.fillRect(x(a),p.coolY,Math.max(1,x(b)-x(a)),14)}
    if(seriesVisibility.fan&&row.fan_request){c.fillStyle="#a8cf45";c.fillRect(x(a),p.fanY,Math.max(1,x(b)-x(a)),14)}
  }
  if(!dataRows.length){c.fillStyle="#66736f";c.font="13px system-ui";c.textAlign="center";c.fillText("No samples in this time window",(p.l+W-p.r)/2,(p.t+p.tempB)/2)}
  if(hoverTime!=null){const X=x(hoverTime),bottom=seriesVisibility.fan?p.fanY+14:seriesVisibility.cooling?p.coolY+14:p.tempB;if(X>=p.l&&X<=W-p.r){c.strokeStyle=hoverEvent?"#bb3d32":"#15201d99";c.lineWidth=hoverEvent?2:1;c.beginPath();c.moveTo(X,p.t);c.lineTo(X,bottom);c.stroke()}}
}

function setGraphView(start,end,live=false){
  const full=graphDomain(),fullSpan=full.end-full.start,span=Math.min(fullSpan,Math.max(2*60000,end-start));
  if(live){end=full.end;start=end-span}
  if(start<full.start){start=full.start;end=start+span}
  if(end>full.end){end=full.end;start=end-span}
  graphView=span>=fullSpan-1000?null:{start,end,live};drawChart();
}
function zoomGraph(factor,anchor=1){
  const g=graphGeometry;if(!g)return;
  const oldSpan=g.t1-g.t0,newSpan=Math.max(2*60000,Math.min(g.full.end-g.full.start,oldSpan*factor));
  const fixed=g.t0+oldSpan*anchor;
  setGraphView(fixed-newSpan*anchor,fixed+newSpan*(1-anchor),anchor>.98&&Math.abs(g.t1-g.full.end)<10000);
}
function showEventInspector(events){
  const panel=$("#eventInspector");
  if(!events.length){panel.classList.add("hidden");return}
  const shown=events.slice(0,8);
  panel.innerHTML=`<div class="event-inspector-header"><b>Thermostat event${events.length===1?"":"s"}</b><span>${events.length===1?"Selected marker":`${events.length} nearby markers`}</span></div>${shown.map(event=>`<div class="event-inspector-row"><time>${new Date(event._t).toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit",second:"2-digit"})}</time><div><b>${escapeHtml(event.kind)}</b>${escapeHtml(event.note||"Value changed")}</div></div>`).join("")}`;
  panel.classList.remove("hidden");
  panel.scrollIntoView({block:"nearest",behavior:"smooth"});
}
function showGraphTooltip(clientX,clientY,persistent=false){
  const g=graphGeometry,canvas=$("#chart"),tip=$("#chartTooltip");if(!g)return;
  const r=canvas.getBoundingClientRect(),px=Math.max(g.p.l,Math.min(g.W-g.p.r,clientX-r.left));
  const pointerTime=g.t0+(px-g.p.l)/(g.W-g.p.l-g.p.r)*(g.t1-g.t0);
  const events=(seriesVisibility.events?history.observations.filter(isGraphEvent):[]).map(event=>({...event,_t:new Date(event.occurred_at).getTime()})).filter(event=>event._t>=g.t0&&event._t<=g.t1);
  const hitRadius=persistent?32:12;
  const nearbyEvents=events.filter(event=>Math.abs(g.x(event._t)-px)<=hitRadius).sort((a,b)=>a._t-b._t);
  const nearestEvent=nearbyEvents.length?nearbyEvents.reduce((a,b)=>Math.abs(g.x(b._t)-px)<Math.abs(g.x(a._t)-px)?b:a):null;
  if(nearestEvent){
    hoverTime=nearestEvent._t;hoverEvent=nearestEvent;
    tip.innerHTML=`<b>${new Date(nearestEvent._t).toLocaleString()}</b><br><span class="tooltip-event">Thermostat event</span><br>${escapeHtml(nearestEvent.kind)}: ${escapeHtml(nearestEvent.note||"Value changed")}`;
    if(persistent)showEventInspector(nearbyEvents);
  }else{
    hoverTime=pointerTime;hoverEvent=null;
    if(persistent)showEventInspector([]);
    const rows=sortedSamples().filter(row=>row._t>=g.t0&&row._t<=g.t1);
    if(!rows.length){tip.classList.add("hidden");drawChart();return}
    const sample=rows.reduce((a,b)=>Math.abs(b._t-hoverTime)<Math.abs(a._t-hoverTime)?b:a);
    const allowed=Math.max(2*60000,(g.t1-g.t0)/15);
    if(Math.abs(sample._t-hoverTime)>allowed){tip.classList.add("hidden");drawChart();return}
    const values=[];
    if(seriesVisibility.temperature)values.push(`Measured ${fmt(sample.indoor_temp,1)}°`);
    if(seriesVisibility.setpoint)values.push(`Setpoint ${fmt(sample.cool_setpoint,1)}°`);
    if(seriesVisibility.humidity)values.push(`Humidity ${fmt(sample.indoor_humidity,0)}%`);
    const equipment=[];
    if(seriesVisibility.cooling)equipment.push(friendlyMode(sample.operation_mode));
    if(seriesVisibility.fan)equipment.push(`Blower ${sample.fan_request?"requested (inferred)":"not inferred"}`);
    tip.innerHTML=`<b>${new Date(sample._t).toLocaleString()}</b><br>${values.join(" · ")}${equipment.length?`<br>${equipment.join(" · ")}`:""}`;
  }
  tip.style.left=`${px}px`;tip.style.top=`${clientY-r.top+canvas.offsetTop}px`;tip.style.transform=px>r.width*.62?"translate(-102%,-105%)":"translate(8px,-105%)";tip.classList.remove("hidden");drawChart();
}
function drawTimeline(){
  const events=[
    ...history.transitions.map(x=>({at:x.occurred_at,title:friendlyMode(x.field),detail:`${friendlyMode(x.from_value)} → ${friendlyMode(x.to_value)}`})),
    ...history.observations.map(x=>({at:x.occurred_at,title:friendlyMode(x.kind),detail:x.note||"Physical observation"}))
  ].sort((a,b)=>new Date(b.at)-new Date(a.at)).slice(0,40);
  $("#timeline").innerHTML=events.length?events.map(e=>`<div class="event"><time>${new Date(e.at).toLocaleString()}</time><div><b>${escapeHtml(e.title)}</b><span>${escapeHtml(e.detail)}</span></div></div>`).join(""):'<p class="muted">No transitions recorded yet.</p>';
}
function escapeHtml(s){const d=document.createElement("div");d.textContent=s??"";return d.innerHTML}

document.querySelectorAll(".range").forEach(b=>b.onclick=async()=>{
  document.querySelectorAll(".range").forEach(x=>x.classList.remove("active"));b.classList.add("active");hours=Number(b.dataset.hours);graphView=null;history=await get(useHomekit ? `/api/homekit/history?hours=${hours}` : `/api/history?hours=${hours}`);drawChart();drawTimeline();
});
document.querySelectorAll("[data-kind]").forEach(b=>b.onclick=async()=>{
  await get("/api/observations",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:b.dataset.kind,note:$("#note").value})});
  b.textContent="Marked ✓";setTimeout(()=>b.textContent=friendlyMode(b.dataset.kind),1300);$("#note").value="";await load();
});
$("#pollNow").onclick=async()=>{const b=$("#pollNow");b.disabled=true;b.textContent="Polling…";try{if(!useHomekit)await get("/api/poll",{method:"POST"});await load()}catch(e){alert(e.message)}finally{b.disabled=false;b.textContent="Poll now"}};
addEventListener("resize",drawChart);
load().catch(e=>{$("#connection").textContent=e.message});
setInterval(()=>load().catch(()=>{}),5000);


function hkLabel(type){const k=String(type).slice(0,8).toUpperCase();return ({"0000000F":"HVAC state","00000011":"Temperature","00000010":"Humidity","00000035":"Target temperature","0000000D":"Cooling threshold","00000012":"Heating threshold","00000033":"Target mode"})[k]||"HomeKit event"}
function hkEventDetail(e){const k=String(e.characteristic).slice(0,8).toUpperCase();let v=e.value_json;try{v=JSON.parse(v)}catch(_){}const n=Number(v);if(k==="0000000F")return (["Inactive","Heating","Cooling"])[n]||`State ${v}`;if(k==="00000033")return (["Off","Heat","Cool","Auto"])[n]||`Mode ${v}`;if(["00000011","00000035","0000000D","00000012"].includes(k)&&Number.isFinite(n))return `${(n*9/5+32).toFixed(1)}°F`;if(k==="00000010"&&Number.isFinite(n))return `${n.toFixed(0)}%`;return `Value ${String(v)}`}
function homekitReading(hk){
  if(!hk.current?.length)return null;
  const main=hk.current.filter(x=>x.service_type?.startsWith("0000004A"));
  const value=prefix=>main.find(x=>x.type?.startsWith(prefix))?.value;
  const units=value("00000036"), cv=v=>v==null?null:(units===1?v*9/5+32:v);
  const state=value("0000000F"), target=value("00000033");
  const modes=["EquipmentOff","Heating","Cooling"];
  return {captured_at:hk.events?.[0]?.occurred_at||new Date().toISOString(),indoor_temp:cv(value("00000011")),indoor_humidity:value("00000010"),cool_setpoint:cv(target===3?value("0000000D"):value("00000035")),heat_setpoint:cv(target===3?value("00000012"):value("00000035")),operation_mode:modes[state]||"Unknown",fan_request:[1,2].includes(state),fan_source:"inferred_from_hvac",system_mode:["Off","Heat","Cool","Auto"][target]||"Unknown",is_alive:true};
}

function renderHomekit(hk){
  const paired=Boolean(hk.paired);
  $("#homekitState").textContent=paired?"Paired locally":hk.ready?"Not paired":"Unavailable";
  $("#homekitState").classList.toggle("online",paired);
  $("#homekitSetup").classList.toggle("hidden",paired);
  $("#pairForm").classList.add("hidden");
  const error=hk.last_error||""; $("#homekitError").textContent=error; $("#homekitError").classList.toggle("hidden",!error);
}

$("#discoverHomekit").onclick=async()=>{
  const b=$("#discoverHomekit"); b.disabled=true; b.textContent="Discovering…";
  try{
    const result=await get("/api/homekit/discover");
    const devices=result.devices.filter(d=>!d.paired);
    $("#homekitDevices").innerHTML=devices.length?devices.map(d=>`<div class="device-choice"><div><b>${escapeHtml(d.name)}</b><br><small>${escapeHtml(d.model||d.id)}</small></div><button data-hkid="${escapeHtml(d.id)}">Select</button></div>`).join(""):"<p class=\"muted\">No unpaired HomeKit accessories found. Keep the T10 pairing code visible and try again.</p>";
    document.querySelectorAll("[data-hkid]").forEach(x=>x.onclick=()=>{selectedHomekitDevice=x.dataset.hkid;$("#pairForm").classList.remove("hidden");$("#pairingCode").focus()});
  }catch(e){$("#homekitError").textContent=e.message;$("#homekitError").classList.remove("hidden")}
  finally{b.disabled=false;b.textContent="Discover T10"}
};

$("#pairHomekit").onclick=async()=>{
  if(!selectedHomekitDevice)return;
  const b=$("#pairHomekit");b.disabled=true;b.textContent="Pairing…";
  try{await get("/api/homekit/pair",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({device_id:selectedHomekitDevice,code:$("#pairingCode").value})});await load()}
  catch(e){$("#homekitError").textContent=e.message;$("#homekitError").classList.remove("hidden")}
  finally{b.disabled=false;b.textContent="Pair locally"}
};

const chartCanvas=$("#chart");
chartCanvas.addEventListener("wheel",e=>{
  e.preventDefault();if(!graphGeometry)return;
  const r=chartCanvas.getBoundingClientRect(),g=graphGeometry;
  const anchor=Math.max(0,Math.min(1,(e.clientX-r.left-g.p.l)/(g.W-g.p.l-g.p.r)));
  zoomGraph(e.deltaY>0?1.35:.72,anchor);
},{passive:false});
chartCanvas.addEventListener("pointerdown",e=>{
  if(!graphGeometry)return;
  dragState={x:e.clientX,y:e.clientY,start:graphGeometry.t0,end:graphGeometry.t1,moved:false};
  chartCanvas.setPointerCapture(e.pointerId);chartCanvas.classList.add("dragging");
});
chartCanvas.addEventListener("pointermove",e=>{
  if(dragState&&graphGeometry){
    const dx=e.clientX-dragState.x,dy=e.clientY-dragState.y;
    dragState.moved=dragState.moved||Math.hypot(dx,dy)>12;
    if(dragState.moved){
      const plotWidth=graphGeometry.W-graphGeometry.p.l-graphGeometry.p.r;
      const delta=-dx/plotWidth*(dragState.end-dragState.start);
      setGraphView(dragState.start+delta,dragState.end+delta,false);
    }
  }else showGraphTooltip(e.clientX,e.clientY);
});
function endChartDrag(e){const wasTap=dragState&&!dragState.moved;dragState=null;chartCanvas.classList.remove("dragging");if(wasTap)showGraphTooltip(e.clientX,e.clientY,true)}
chartCanvas.addEventListener("pointerup",endChartDrag);
chartCanvas.addEventListener("pointercancel",()=>{dragState=null;chartCanvas.classList.remove("dragging")});
chartCanvas.addEventListener("pointerleave",()=>{if(!dragState){hoverTime=null;hoverEvent=null;$("#chartTooltip").classList.add("hidden");drawChart()}});
function jumpLatest(){
  if(!graphGeometry){graphView=null;drawChart();return}
  const span=graphGeometry.t1-graphGeometry.t0,full=graphDomain();
  setGraphView(full.end-span,full.end,true);
}
chartCanvas.addEventListener("dblclick",jumpLatest);
$("#zoomIn").onclick=()=>zoomGraph(.6,1);
$("#zoomOut").onclick=()=>zoomGraph(1.65,1);
$("#resetZoom").onclick=()=>{graphView=null;drawChart()};
$("#jumpLatest").onclick=jumpLatest;


/**
 * Reusable per-thermostat detail module.
 * Data contract: { device, snapshot, history: {samples: []}, source }.
 * Samples need captured_at and operation_mode; comfort fields are optional.
 */
class ThermostatDetailModule {
  constructor(root){
    this.root=root;
    this.data={device:{},snapshot:null,history:{samples:[]},source:"Unknown"};
  }
  setData(data){
    this.data={...this.data,...data};
    const name=data.device?.device_name||data.device?.name||"Thermostat";
    $("#summaryDeviceName").textContent=name;
    $("#detailDeviceName").textContent=name;
    $("#dataSource").textContent=data.source||"Unknown source";
    $("#summaryLocation").textContent=data.device?.location_name
      ? `${data.device.location_name} · Live comfort and equipment status`
      : "Live comfort and equipment status";
    this.renderRuntime();
  }
  renderRuntime(){
    const days=runtimeDays;
    const requested=$("#runtimeBucket").value;
    const bucket=requested==="auto"?(days>90?"month":"day"):requested;
    const result=aggregateRuntime(this.data.history?.samples||[],days,bucket);
    const chart=$("#runtimeChart");
    $("#periodRuntime").textContent=formatRuntime(result.runtimeMs);
    $("#averageRuntime").textContent=formatRuntime(result.runtimeMs/Math.max(1,days));
    $("#periodCycles").textContent=String(result.cycles);
    $("#runtimeCoverage").textContent=`${Math.round(result.coverage*100)}%`;
    $("#runtimeGrouping").textContent=bucket==="month"?"Monthly runtime":"Daily runtime";
    $("#runtimeStart").textContent=new Date(result.start).toLocaleDateString(undefined,{month:"short",day:"numeric",year:days>180?"numeric":undefined});
    $("#runtimeEnd").textContent="Today";
    $("#runtimeEmpty").classList.toggle("hidden",result.buckets.some(x=>x.runtimeMs>0));
    const max=Math.max(1,...result.buckets.map(x=>x.runtimeMs));
    chart.innerHTML=result.buckets.map(item=>{
      const height=Math.max(item.runtimeMs?2:0,Math.round(item.runtimeMs/max*100));
      const label=item.start.toLocaleDateString(undefined,bucket==="month"?{month:"long",year:"numeric"}:{weekday:"short",month:"short",day:"numeric"});
      return `<div class="runtime-column" tabindex="0" style="--bar-height:${height}%"><span class="runtime-bar" style="height:${height}%"></span><span class="runtime-tip">${escapeHtml(label)} · ${formatRuntime(item.runtimeMs)}</span></div>`;
    }).join("");
    chart.setAttribute("aria-label",`${bucket==="month"?"Monthly":"Daily"} cooling runtime for the last ${days} days. Total ${formatRuntime(result.runtimeMs)}.`);
  }
}

function formatRuntime(ms){
  if(!Number.isFinite(ms)||ms<=0)return "0m";
  const minutes=Math.round(ms/60000);
  if(minutes<60)return `${minutes}m`;
  const h=Math.floor(minutes/60),m=minutes%60;
  return m?`${h}h ${m}m`:`${h}h`;
}
function cooling(row){return String(row?.operation_mode||"").toLowerCase().includes("cool")}
function bucketFloor(date,kind){
  return kind==="month"?new Date(date.getFullYear(),date.getMonth(),1):new Date(date.getFullYear(),date.getMonth(),date.getDate());
}
function bucketNext(date,kind){
  return kind==="month"?new Date(date.getFullYear(),date.getMonth()+1,1):new Date(date.getFullYear(),date.getMonth(),date.getDate()+1);
}
function aggregateRuntime(samples,days,bucketKind){
  const end=Date.now(),start=end-days*86400000;
  const rows=samples.map(x=>({...x,_t:new Date(x.captured_at).getTime()})).filter(x=>Number.isFinite(x._t)&&x._t>=start-86400000&&x._t<=end).sort((a,b)=>a._t-b._t);
  const deltas=rows.slice(1).map((row,i)=>row._t-rows[i]._t).filter(x=>x>0&&x<6*3600000).sort((a,b)=>a-b);
  const median=deltas.length?deltas[Math.floor(deltas.length/2)]:5*60000;
  const gapLimit=useHomekit?6*3600000:Math.max(10*60000,Math.min(30*60000,median*3));
  const buckets=[];
  let cursor=bucketFloor(new Date(start),bucketKind);
  while(cursor.getTime()<end){
    const next=bucketNext(cursor,bucketKind);
    buckets.push({start:new Date(cursor),end:next.getTime(),runtimeMs:0});
    cursor=next;
  }
  let runtimeMs=0,coverageMs=0,cycles=0,previousCooling=false;
  for(let i=0;i<rows.length;i++){
    const row=rows[i],isCooling=cooling(row);
    if(isCooling&&!previousCooling&&row._t>=start)cycles++;
    previousCooling=isCooling;
    if(i===rows.length-1)continue;
    let a=Math.max(start,row._t),b=Math.min(end,rows[i+1]._t);
    const rawGap=rows[i+1]._t-row._t;
    if(b<=a||rawGap>gapLimit)continue;
    coverageMs+=b-a;
    if(!isCooling)continue;
    runtimeMs+=b-a;
    for(const item of buckets){
      const overlap=Math.max(0,Math.min(b,item.end)-Math.max(a,item.start.getTime()));
      item.runtimeMs+=overlap;
    }
  }
  return {start,end,runtimeMs,cycles,coverage:Math.min(1,coverageMs/(days*86400000)),buckets};
}

const detailModule=new ThermostatDetailModule($("#dashboard"));
window.ThermostatDetailModule=ThermostatDetailModule;
window.aggregateThermostatRuntime=aggregateRuntime;

async function loadRuntimeData(force){
  if(!force&&runtimeHistory.samples.length&&Date.now()-runtimeLoadedAt<60000)return;
  runtimeHistory=await get(useHomekit?`/api/homekit/history?hours=${runtimeDays*24}`:`/api/history?hours=${runtimeDays*24}`);
  runtimeLoadedAt=Date.now();
}
async function changeRuntimeRange(){
  runtimeDays=Number($("#runtimeDuration").value);
  runtimeLoadedAt=0;
  await loadRuntimeData(true);
  detailModule.data.history=runtimeHistory;
  detailModule.renderRuntime();
}
$("#runtimeDuration").onchange=()=>changeRuntimeRange().catch(e=>{$("#runtimeEmpty").textContent=e.message;$("#runtimeEmpty").classList.remove("hidden")});
$("#runtimeBucket").onchange=()=>detailModule.renderRuntime();
$("#pollNowTop").onclick=()=>$("#pollNow").click();
