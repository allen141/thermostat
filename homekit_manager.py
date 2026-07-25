"""Multi-accessory local HomeKit collector."""
from __future__ import annotations
import asyncio,json,os,re,sqlite3,threading
from datetime import datetime,timezone
from pathlib import Path

def utcnow(): return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
def slug(v): return re.sub(r"[^a-z0-9]+","-",str(v).lower()).strip("-") or "homekit"

class HomeKitManager:
 def __init__(self,data_dir:Path):
  self.data_dir=data_dir; self.pairings_file=data_dir/"homekit-pairings.json"; self.db_path=data_dir/"thermostat.sqlite"
  self.loop=self.controller=None; self.discoveries={}; self.characteristics={}; self.accessory_meta={}; self.pairings={}; self.poll_state={}; self.last_error=None
  self.poll_seconds=max(0,int(os.environ.get("HOMEKIT_POLL_SECONDS","30"))); self.ready=threading.Event(); self.thread=threading.Thread(target=self._thread_main,daemon=True)
 def start(self): self.thread.start(); self.ready.wait(15)
 def _thread_main(self):
  self.loop=asyncio.new_event_loop(); asyncio.set_event_loop(self.loop); self.loop.run_until_complete(self._main())
 async def _main(self):
  try:
   from aiohomekit import Controller
   from aiohomekit.zeroconf import HAP_TYPE_TCP,HAP_TYPE_UDP
   from zeroconf.asyncio import AsyncServiceBrowser,AsyncZeroconf
   self.zeroconf=AsyncZeroconf(); self.browser=AsyncServiceBrowser(self.zeroconf.zeroconf,[HAP_TYPE_TCP,HAP_TYPE_UDP],handlers=[lambda **kwargs:None])
   self.controller=Controller(async_zeroconf_instance=self.zeroconf); await self.controller.async_start(); self.controller.load_data(str(self.pairings_file))
   for alias,pairing in list(self.controller.aliases.items()): await self._setup_pairing(alias,pairing)
  except Exception as exc: self.last_error=str(exc)
  finally:
   if self.poll_seconds:self.loop.create_task(self._heartbeat())
   self.ready.set()
  await asyncio.Event().wait()
 def run(self,coro,timeout=30):
  if not self.loop or not self.controller: raise RuntimeError("HomeKit controller is not ready")
  return asyncio.run_coroutine_threadsafe(coro,self.loop).result(timeout)
 def _source(self,alias):
  with sqlite3.connect(self.db_path,timeout=30) as conn:
   conn.row_factory=sqlite3.Row; row=conn.execute("SELECT * FROM unit_sources WHERE kind='homekit' AND external_id=?",(alias,)).fetchone()
  return dict(row) if row else None
 def _ensure_source(self,alias,unit_id=None,name=None,model=None):
  found=self._source(alias)
  if found:return found
  text=f"{alias} {name or ''} {model or ''}".lower(); unit_id=unit_id or ("sensi" if "sensi" in text or "1f95" in text else "t10"); source_id=f"{unit_id}-homekit"
  with sqlite3.connect(self.db_path,timeout=30) as conn:
   conn.execute("INSERT OR IGNORE INTO units(id,display_name,vendor,model,created_at) VALUES(?,?,?,?,?)",(unit_id,"Sensi" if unit_id=="sensi" else "T10","Copeland" if unit_id=="sensi" else "Resideo",model or ("1F95U-42WF" if unit_id=="sensi" else "T10"),utcnow()))
   conn.execute("INSERT INTO unit_sources(id,unit_id,kind,external_id,priority,enabled,created_at) VALUES(?,?,?,?,100,1,?) ON CONFLICT(id) DO UPDATE SET external_id=excluded.external_id,enabled=1",(source_id,unit_id,"homekit",alias,utcnow()))
  return self._source(alias)
 def status(self,unit_id=None):
  items=[]
  for alias in (list(self.controller.aliases) if self.controller else []):
   source=self._source(alias)
   if unit_id and (not source or source["unit_id"]!=unit_id):continue
   state=self.poll_state.get(alias,{})
   items.append({"alias":alias,"unit_id":source["unit_id"] if source else None,"name":self.accessory_meta.get(alias,{}).get("name",alias),"model":self.accessory_meta.get(alias,{}).get("model",""),"current":[dict(v) for (a,_aid,_iid),v in self.characteristics.items() if a==alias],"last_poll_at":state.get("last_poll_at"),"last_poll_error":state.get("last_poll_error")})
  return {"ready":self.ready.is_set(),"paired":bool(items),"aliases":[x["alias"] for x in items],"accessories":items,"characteristic_count":sum(len(x["current"]) for x in items),"current":[v for x in items for v in x["current"]],"last_error":self.last_error,"collection_mode":"events_and_polling" if self.poll_seconds else "events_only","poll_seconds":self.poll_seconds}
 async def _discover(self):
  found=[]; self.discoveries={}
  async for d in self.controller.async_discover(timeout=8):
   desc=d.description; did=str(getattr(desc,"id","")); self.discoveries[did]=d; found.append({"id":did,"name":str(getattr(desc,"name","HomeKit accessory")),"model":str(getattr(desc,"model","")),"paired":d.paired,"category":str(getattr(desc,"category",""))})
  return found
 def discover(self):return self.run(self._discover(),20)
 async def _pair(self,device_id,code,unit_id):
  d=self.discoveries.get(device_id)
  if not d: await self._discover(); d=self.discoveries.get(device_id)
  if not d:raise RuntimeError("Accessory is no longer discoverable; reopen Connect HomeKit")
  if d.paired:raise RuntimeError("Accessory is already paired. Do not reset it automatically; remove it from the other controller or enable pairing there first.")
  desc=d.description; name=str(getattr(desc,"name","HomeKit accessory")); model=str(getattr(desc,"model","")); base=slug(f"{unit_id or model or name}-{device_id[-6:]}"); alias=base; n=2
  while alias in self.controller.aliases:alias=f"{base}-{n}";n+=1
  finish=await d.async_start_pairing(alias); pairing=await finish(code); self.controller.aliases[alias]=pairing; self.controller.pairings[pairing.id]=pairing; self.controller.save_data(str(self.pairings_file)); self._ensure_source(alias,unit_id,name,model); await self._setup_pairing(alias,pairing,name,model); return self.status()
 def pair(self,device_id,code,unit_id=None):
  digits=re.sub(r"\D","",code)
  if not re.fullmatch(r"\d{8}",digits):raise ValueError("Enter the eight digits shown on the thermostat")
  if unit_id not in (None,"t10","sensi"):raise ValueError("Unknown thermostat unit")
  return self.run(self._pair(device_id,f"{digits[:3]}-{digits[3:5]}-{digits[5:]}",unit_id),90)
 async def _setup_pairing(self,alias,pairing,name=None,model=None):
  self.pairings[alias]=pairing; self._ensure_source(alias,name=name,model=model); accessories=await pairing.list_accessories_and_characteristics(); events=set(); self.accessory_meta[alias]={"name":name or alias,"model":model or ""}
  for accessory in accessories:
   aid=accessory["aid"]
   for service in accessory.get("services",[]):
    for char in service.get("characteristics",[]):
     pk=(aid,char["iid"]); key=(alias,*pk); meta={"alias":alias,"aid":aid,"iid":char["iid"],"type":char.get("type"),"description":char.get("description") or char.get("type"),"service_type":service.get("type"),"value":char.get("value"),"perms":char.get("perms",[])}; self.characteristics[key]=meta; self._store_event(alias,meta,"snapshot")
     if "ev" in char.get("perms",[]):events.add(pk)
  self._store_sample(alias,"snapshot"); pairing.dispatcher_connect(lambda changes,a=alias:self._handle_changes(a,changes))
  if events:await pairing.subscribe(events)
  self.last_error=None
 def _handle_changes(self,alias,changes):
  for pk,change in changes.items():
   key=(alias,pk[0],pk[1]); meta=dict(self.characteristics.get(key,{"alias":alias,"aid":pk[0],"iid":pk[1],"description":"Unknown characteristic"})); meta.update(change if isinstance(change,dict) else {"value":change}); self.characteristics.setdefault(key,meta).update(value=meta.get("value")); self._store_event(alias,meta,"event")
  self._store_sample(alias,"event")
 def _store_event(self,alias,payload,event_source):
  safe=json.loads(json.dumps(payload,default=str)); source=self._ensure_source(alias)
  with sqlite3.connect(self.db_path,timeout=30) as conn:
   conn.execute("CREATE TABLE IF NOT EXISTS homekit_events(id INTEGER PRIMARY KEY,occurred_at TEXT NOT NULL,alias TEXT NOT NULL,aid INTEGER,iid INTEGER,characteristic TEXT,value_json TEXT,source TEXT NOT NULL,raw_json TEXT NOT NULL)")
   conn.execute("INSERT INTO homekit_events(occurred_at,alias,aid,iid,characteristic,value_json,source,raw_json) VALUES(?,?,?,?,?,?,?,?)",(utcnow(),alias,safe.get("aid"),safe.get("iid"),safe.get("description") or safe.get("type"),json.dumps(safe.get("value")),event_source,json.dumps({**safe,"unit_id":source["unit_id"]},separators=(",",":"))))
 async def _poll_pairing(self,alias,pairing):
  chars={(aid,iid):meta for (a,aid,iid),meta in self.characteristics.items() if a==alias}; keys={k for k,m in chars.items() if str(m.get("service_type","")).startswith("0000004A") and "pr" in m.get("perms",[])}
  if not keys:raise RuntimeError("No readable thermostat characteristics found")
  readings=await pairing.get_characteristics(keys); errors=[]; updated=0
  for key,result in readings.items():
   if result.get("status",0):errors.append(f"{key[0]}.{key[1]} status {result['status']}");continue
   target=(alias,*key)
   if "value" in result and target in self.characteristics:self.characteristics[target]["value"]=result["value"];updated+=1
  if not updated:raise RuntimeError("Thermostat read returned no values"+(f": {', '.join(errors)}" if errors else ""))
  self._store_sample(alias,"poll"); self.poll_state[alias]={"last_poll_at":utcnow(),"last_poll_error":"; ".join(errors) if errors else None}
 async def _heartbeat(self):
  while True:
   await asyncio.sleep(self.poll_seconds)
   for alias,pairing in list(self.pairings.items()):
    try:await self._poll_pairing(alias,pairing)
    except Exception as exc:self.poll_state.setdefault(alias,{})["last_poll_error"]=str(exc)
 def poll(self,unit_id):
  with sqlite3.connect(self.db_path,timeout=30) as conn:row=conn.execute("SELECT external_id FROM unit_sources WHERE unit_id=? AND kind='homekit' AND enabled=1",(unit_id,)).fetchone()
  if not row or row[0] not in self.pairings:raise RuntimeError(f"{unit_id} is not paired through HomeKit")
  self.run(self._poll_pairing(row[0],self.pairings[row[0]]),30);return self.status(unit_id)
 def _values(self,alias):
  chars=[v for (a,_aid,_iid),v in self.characteristics.items() if a==alias and str(v.get("service_type","")).startswith("0000004A")]
  def val(prefix):
   item=next((x for x in chars if str(x.get("type","")).startswith(prefix)),None);return item.get("value") if item else None
  return {"current_temp_c":val("00000011"),"humidity":val("00000010"),"target_temp_c":val("00000035"),"cooling_threshold_c":val("0000000D"),"heating_threshold_c":val("00000012"),"current_hvac":val("0000000F"),"target_hvac":val("00000033"),"display_units":val("00000036")}
 def _store_sample(self,alias,event_source):
  v=self._values(alias)
  if v["current_temp_c"] is None:return
  source=self._ensure_source(alias); cv=lambda x:None if x is None else x*9/5+32; target=v["cooling_threshold_c"] if v["target_hvac"]==3 else v["target_temp_c"]; hvac=v["current_hvac"]; op=["EquipmentOff","Heating","Cooling"][hvac] if hvac in (0,1,2) else "Unknown"; captured=utcnow()
  with sqlite3.connect(self.db_path,timeout=30) as conn:
   cur=conn.execute("INSERT INTO telemetry_samples(captured_at,unit_id,source_id,indoor_temp,indoor_humidity,cool_setpoint,heat_setpoint,system_mode,operation_mode,fan_request,is_alive,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(captured,source["unit_id"],source["id"],cv(v["current_temp_c"]),v["humidity"],cv(target),cv(v["heating_threshold_c"] or v["target_temp_c"]),["Off","Heat","Cool","Auto"][v["target_hvac"]] if v["target_hvac"] in (0,1,2,3) else "Unknown",op,hvac in (1,2),1,json.dumps({"alias":alias,"event_source":event_source,**v},separators=(",",":"))))
   current=conn.execute("SELECT * FROM telemetry_samples WHERE id=?",(cur.lastrowid,)).fetchone();previous=conn.execute("SELECT * FROM telemetry_samples WHERE unit_id=? AND source_id=? AND id<>? ORDER BY id DESC LIMIT 1",(source["unit_id"],source["id"],cur.lastrowid)).fetchone()
   if previous:
    for field in ("operation_mode","fan_request","system_mode","cool_setpoint","heat_setpoint","is_alive"):
     if previous[field]!=current[field]:conn.execute("INSERT INTO unit_transitions(occurred_at,unit_id,source_id,field,from_value,to_value,sample_id) VALUES(?,?,?,?,?,?,?)",(captured,source["unit_id"],source["id"],field,str(previous[field]),str(current[field]),cur.lastrowid))
 def latest_events(self,limit=100,unit_id=None):
  try:
   with sqlite3.connect(self.db_path,timeout=30) as conn:
    conn.row_factory=sqlite3.Row
    if unit_id:rows=conn.execute("SELECT e.* FROM homekit_events e JOIN unit_sources s ON s.external_id=e.alias WHERE s.kind='homekit' AND s.unit_id=? ORDER BY e.id DESC LIMIT ?",(unit_id,min(limit,500))).fetchall()
    else:rows=conn.execute("SELECT * FROM homekit_events ORDER BY id DESC LIMIT ?",(min(limit,500),)).fetchall()
   return [dict(r) for r in rows]
  except sqlite3.OperationalError:return []
