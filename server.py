#!/usr/bin/env python3
"""Read-only multi-thermostat flight recorder."""
from __future__ import annotations
import base64,csv,hashlib,http.server,io,json,os,secrets,sqlite3,threading,time,urllib.error,urllib.parse,urllib.request
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent;DATA=ROOT/"data";DB_PATH=DATA/"thermostat.sqlite";STATIC=ROOT/"static";API_BASE="https://api.honeywellhome.com";HOMEKIT_MANAGER=None

def load_env():
 p=ROOT/".env"
 if not p.exists():return
 for raw in p.read_text(encoding="utf-8").splitlines():
  line=raw.strip()
  if line and not line.startswith("#") and "=" in line:
   key,value=line.split("=",1);os.environ.setdefault(key.strip(),value.strip().strip("'\""))
load_env();CLIENT_ID=os.getenv("RESIDEO_CLIENT_ID","");CLIENT_SECRET=os.getenv("RESIDEO_CLIENT_SECRET","");REDIRECT_URI=os.getenv("RESIDEO_REDIRECT_URI","http://127.0.0.1:8787/auth/callback");POLL_SECONDS=max(300,int(os.getenv("POLL_SECONDS","300")));HOST=os.getenv("HOST","0.0.0.0");PORT=int(os.getenv("PORT","8787"));DATA.mkdir(exist_ok=True)
def now():return datetime.now(timezone.utc).isoformat(timespec="seconds")
def db():
 conn=sqlite3.connect(DB_PATH,timeout=30);conn.row_factory=sqlite3.Row;conn.execute("PRAGMA journal_mode=WAL");return conn
def table_exists(conn,name):return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())
def columns(conn,name):return {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}

def init_db():
 with db() as conn:
  conn.executescript("""
  CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY,captured_at TEXT NOT NULL,device_id TEXT NOT NULL,indoor_temp REAL,outdoor_temp REAL,indoor_humidity REAL,outdoor_humidity REAL,cool_setpoint REAL,heat_setpoint REAL,system_mode TEXT,operation_mode TEXT,fan_request INTEGER,circulation_fan_request INTEGER,is_alive INTEGER,raw_json TEXT NOT NULL);
  CREATE INDEX IF NOT EXISTS samples_time ON samples(captured_at);
  CREATE TABLE IF NOT EXISTS transitions(id INTEGER PRIMARY KEY,occurred_at TEXT NOT NULL,field TEXT NOT NULL,from_value TEXT,to_value TEXT,sample_id INTEGER REFERENCES samples(id));
  CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,occurred_at TEXT NOT NULL,kind TEXT NOT NULL,note TEXT NOT NULL DEFAULT '');
  CREATE TABLE IF NOT EXISTS auxiliary(id INTEGER PRIMARY KEY,captured_at TEXT NOT NULL,kind TEXT NOT NULL,raw_json TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS units(id TEXT PRIMARY KEY,display_name TEXT NOT NULL,vendor TEXT NOT NULL,model TEXT NOT NULL,created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS unit_sources(id TEXT PRIMARY KEY,unit_id TEXT NOT NULL REFERENCES units(id),kind TEXT NOT NULL,external_id TEXT,priority INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
  CREATE UNIQUE INDEX IF NOT EXISTS unit_source_external ON unit_sources(kind,external_id) WHERE external_id IS NOT NULL;
  CREATE TABLE IF NOT EXISTS telemetry_samples(id INTEGER PRIMARY KEY,captured_at TEXT NOT NULL,unit_id TEXT NOT NULL REFERENCES units(id),source_id TEXT NOT NULL REFERENCES unit_sources(id),source_sample_id TEXT,indoor_temp REAL,outdoor_temp REAL,indoor_humidity REAL,outdoor_humidity REAL,cool_setpoint REAL,heat_setpoint REAL,system_mode TEXT,operation_mode TEXT,fan_request INTEGER,circulation_fan_request INTEGER,is_alive INTEGER,raw_json TEXT NOT NULL);
  CREATE UNIQUE INDEX IF NOT EXISTS telemetry_source_sample ON telemetry_samples(source_id,source_sample_id) WHERE source_sample_id IS NOT NULL;
  CREATE INDEX IF NOT EXISTS telemetry_unit_time ON telemetry_samples(unit_id,captured_at);
  CREATE TABLE IF NOT EXISTS unit_transitions(id INTEGER PRIMARY KEY,occurred_at TEXT NOT NULL,unit_id TEXT NOT NULL,source_id TEXT NOT NULL,source_transition_id TEXT,field TEXT NOT NULL,from_value TEXT,to_value TEXT,sample_id INTEGER REFERENCES telemetry_samples(id));
  """)
  added_observation_unit="unit_id" not in columns(conn,"observations")
  if added_observation_unit:conn.execute("ALTER TABLE observations ADD COLUMN unit_id TEXT REFERENCES units(id)");conn.execute("UPDATE observations SET unit_id='t10' WHERE unit_id IS NULL")
  if "source_transition_id" not in columns(conn,"unit_transitions"):conn.execute("ALTER TABLE unit_transitions ADD COLUMN source_transition_id TEXT")
  conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS unit_transition_source ON unit_transitions(source_id,source_transition_id) WHERE source_transition_id IS NOT NULL")
  conn.execute("INSERT OR IGNORE INTO units VALUES('t10','T10','Resideo','T10',?)",(now(),));conn.execute("INSERT OR IGNORE INTO units VALUES('sensi','Sensi','Copeland','1F95U-42WF',?)",(now(),))
  conn.execute("INSERT OR IGNORE INTO unit_sources VALUES('t10-resideo','t10','resideo',NULL,50,1,?)",(now(),));conn.execute("INSERT OR IGNORE INTO unit_sources VALUES('t10-homekit','t10','homekit','resideo-t10',100,1,?)",(now(),))
  conn.execute("""INSERT OR IGNORE INTO telemetry_samples(captured_at,unit_id,source_id,source_sample_id,indoor_temp,outdoor_temp,indoor_humidity,outdoor_humidity,cool_setpoint,heat_setpoint,system_mode,operation_mode,fan_request,circulation_fan_request,is_alive,raw_json)
   SELECT captured_at,'t10','t10-resideo','legacy:'||id,indoor_temp,outdoor_temp,indoor_humidity,outdoor_humidity,cool_setpoint,heat_setpoint,system_mode,operation_mode,fan_request,circulation_fan_request,is_alive,raw_json FROM samples""")
  conn.execute("""INSERT OR IGNORE INTO unit_transitions(occurred_at,unit_id,source_id,source_transition_id,field,from_value,to_value,sample_id) SELECT tr.occurred_at,'t10','t10-resideo','legacy:'||tr.id,tr.field,tr.from_value,tr.to_value,ts.id FROM transitions tr LEFT JOIN telemetry_samples ts ON ts.source_id='t10-resideo' AND ts.source_sample_id='legacy:'||tr.sample_id""")
  if table_exists(conn,"homekit_samples"):
   cols=columns(conn,"homekit_samples");alias="COALESCE(alias,'resideo-t10')" if "alias" in cols else "'resideo-t10'"
   conn.execute(f"""INSERT OR IGNORE INTO telemetry_samples(captured_at,unit_id,source_id,source_sample_id,indoor_temp,indoor_humidity,cool_setpoint,heat_setpoint,system_mode,operation_mode,fan_request,is_alive,raw_json)
    SELECT captured_at,'t10','t10-homekit','legacy-homekit:'||id,CASE WHEN display_units=1 THEN current_temp_c*9/5+32 ELSE current_temp_c END,humidity,CASE WHEN display_units=1 THEN COALESCE(cooling_threshold_c,target_temp_c)*9/5+32 ELSE COALESCE(cooling_threshold_c,target_temp_c) END,CASE WHEN display_units=1 THEN COALESCE(heating_threshold_c,target_temp_c)*9/5+32 ELSE COALESCE(heating_threshold_c,target_temp_c) END,CASE target_hvac WHEN 0 THEN 'Off' WHEN 1 THEN 'Heat' WHEN 2 THEN 'Cool' WHEN 3 THEN 'Auto' ELSE 'Unknown' END,CASE current_hvac WHEN 0 THEN 'EquipmentOff' WHEN 1 THEN 'Heating' WHEN 2 THEN 'Cooling' ELSE 'Unknown' END,current_hvac IN (1,2),1,json_object('legacy_homekit_alias',{alias},'legacy_source',source) FROM homekit_samples""")

def put_kv(key,value):
 with db() as conn:conn.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(value)))
def get_kv(key,default=None):
 with db() as conn:row=conn.execute("SELECT value FROM kv WHERE key=?",(key,)).fetchone()
 return json.loads(row["value"]) if row else default
def api_request(path,token,params=None):
 query=dict(params or {});query["apikey"]=CLIENT_ID;request=urllib.request.Request(API_BASE+path+"?"+urllib.parse.urlencode(query),headers={"Authorization":f"Bearer {token}","Accept":"application/json"})
 with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)
def token_request(form):
 auth=base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode();request=urllib.request.Request(API_BASE+"/oauth2/token",data=urllib.parse.urlencode(form).encode(),headers={"Authorization":f"Basic {auth}","Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"})
 with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)
def valid_token():
 tokens=get_kv("tokens")
 if not tokens:raise RuntimeError("Resideo account is not connected")
 if time.time()<tokens.get("expires_at",0)-60:return tokens["access_token"]
 fresh=token_request({"grant_type":"refresh_token","refresh_token":tokens["refresh_token"]});fresh.setdefault("refresh_token",tokens["refresh_token"]);fresh["expires_at"]=time.time()+int(fresh.get("expires_in",600));put_kv("tokens",fresh);return fresh["access_token"]
def discover():
 token=valid_token();thermostats=[]
 for location in api_request("/v2/locations",token):
  lid=location.get("locationID") or location.get("locationId")
  for device in api_request("/v2/devices",token,{"locationId":lid}):
   if device.get("deviceClass")=="Thermostat":thermostats.append({"location_id":lid,"location_name":location.get("name","Home"),"device_id":device.get("deviceID"),"device_name":device.get("userDefinedDeviceName") or device.get("name","Thermostat")})
 if not thermostats:raise RuntimeError("No thermostat was found in the connected account")
 selected=thermostats[0];put_kv("devices",thermostats);put_kv("selected_device",selected);return selected
def store_aux(kind,payload):
 with db() as conn:conn.execute("INSERT INTO auxiliary(captured_at,kind,raw_json) VALUES(?,?,?)",(now(),kind,json.dumps(payload,separators=(",",":"))))

def poll_once():
 selected=get_kv("selected_device") or discover();token=valid_token();params={"locationId":selected["location_id"]};device_id=urllib.parse.quote(selected["device_id"],safe="");payload=api_request(f"/v2/devices/thermostats/{device_id}",token,params);cv=payload.get("changeableValues") or {};op=payload.get("operationStatus") or {};captured=now();values=(captured,selected["device_id"],payload.get("indoorTemperature"),payload.get("outdoorTemperature"),payload.get("indoorHumidity"),payload.get("displayedOutdoorHumidity"),cv.get("coolSetpoint"),cv.get("heatSetpoint"),cv.get("mode"),op.get("mode"),bool(op.get("fanRequest")),bool(op.get("circulationFanRequest")),bool(payload.get("isAlive")),json.dumps(payload,separators=(",",":")))
 fields=("captured_at","device_id","indoor_temp","outdoor_temp","indoor_humidity","outdoor_humidity","cool_setpoint","heat_setpoint","system_mode","operation_mode","fan_request","circulation_fan_request","is_alive","raw_json");current=dict(zip(fields,values))
 with db() as conn:
  previous=conn.execute("SELECT * FROM samples WHERE device_id=? ORDER BY id DESC LIMIT 1",(selected["device_id"],)).fetchone();legacy=conn.execute("INSERT INTO samples(captured_at,device_id,indoor_temp,outdoor_temp,indoor_humidity,outdoor_humidity,cool_setpoint,heat_setpoint,system_mode,operation_mode,fan_request,circulation_fan_request,is_alive,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",values);normalized=conn.execute("INSERT INTO telemetry_samples(captured_at,unit_id,source_id,source_sample_id,indoor_temp,outdoor_temp,indoor_humidity,outdoor_humidity,cool_setpoint,heat_setpoint,system_mode,operation_mode,fan_request,circulation_fan_request,is_alive,raw_json) VALUES(?,'t10','t10-resideo',?,?,?,?,?,?,?,?,?,?,?,?,?)",(captured,f"live:{legacy.lastrowid}",*values[2:]))
  if previous:
   for field in ("operation_mode","fan_request","circulation_fan_request","system_mode","cool_setpoint","heat_setpoint","is_alive"):
    if previous[field]!=current[field]:conn.execute("INSERT INTO transitions(occurred_at,field,from_value,to_value,sample_id) VALUES(?,?,?,?,?)",(captured,field,str(previous[field]),str(current[field]),legacy.lastrowid));conn.execute("INSERT INTO unit_transitions(occurred_at,unit_id,source_id,field,from_value,to_value,sample_id) VALUES(?,'t10','t10-resideo',?,?,?,?)",(captured,field,str(previous[field]),str(current[field]),normalized.lastrowid))
 if time.time()-get_kv("last_config_poll",0)>86400:
  try:store_aux("configuration",api_request(f"/v2/devices/thermostats/{device_id}/thermostatconfiguration",token,params));put_kv("last_config_poll",time.time())
  except Exception as exc:put_kv("last_aux_error",f"{now()} configuration: {exc}")
 put_kv("last_poll",{"at":captured,"ok":True});return payload
def collector():
 while True:
  try:
   if get_kv("tokens"):poll_once()
  except Exception as exc:put_kv("last_poll",{"at":now(),"ok":False,"error":str(exc)})
  time.sleep(POLL_SECONDS)
def rows(rows):return [dict(row) for row in rows]
def unit_exists(unit_id):
 with db() as conn:return bool(conn.execute("SELECT 1 FROM units WHERE id=?",(unit_id,)).fetchone())
def homekit_paired(unit_id):return bool(HOMEKIT_MANAGER and HOMEKIT_MANAGER.status(unit_id).get("paired"))
def active_source(unit_id):
 if homekit_paired(unit_id):return f"{unit_id}-homekit"
 if unit_id=="t10" and get_kv("tokens"):return "t10-resideo"
 with db() as conn:
  row=conn.execute("SELECT source_id FROM telemetry_samples WHERE unit_id=? ORDER BY id DESC LIMIT 1",(unit_id,)).fetchone()
 return row[0] if row else f"{unit_id}-homekit"
def cutoff_for(hours):return datetime.fromtimestamp(time.time()-hours*3600,timezone.utc).isoformat(timespec="seconds")
def history_for(unit_id,hours):
 if not unit_exists(unit_id):raise KeyError("Unknown thermostat unit")
 cutoff=cutoff_for(hours);source=active_source(unit_id)
 with db() as conn:
  samples=rows(conn.execute("SELECT id,captured_at,unit_id,source_id,indoor_temp,outdoor_temp,indoor_humidity,cool_setpoint,heat_setpoint,system_mode,operation_mode,fan_request,is_alive FROM telemetry_samples WHERE unit_id=? AND source_id=? AND captured_at>=? ORDER BY id",(unit_id,source,cutoff)))
  if not samples:
   samples=rows(conn.execute("SELECT id,captured_at,unit_id,source_id,indoor_temp,outdoor_temp,indoor_humidity,cool_setpoint,heat_setpoint,system_mode,operation_mode,fan_request,is_alive FROM telemetry_samples WHERE unit_id=? AND captured_at>=? ORDER BY id",(unit_id,cutoff)))
   if samples:source=samples[-1]["source_id"];samples=[x for x in samples if x["source_id"]==source]
  transitions=rows(conn.execute("SELECT * FROM unit_transitions WHERE unit_id=? AND source_id=? AND occurred_at>=? ORDER BY id",(unit_id,source,cutoff)))
  observations=rows(conn.execute("SELECT * FROM observations WHERE (unit_id=? OR unit_id IS NULL) AND occurred_at>=? ORDER BY id",(unit_id,cutoff)))
  if table_exists(conn,"homekit_events"):
   events=rows(conn.execute("SELECT e.occurred_at,e.characteristic,e.value_json,e.source FROM homekit_events e JOIN unit_sources s ON s.external_id=e.alias WHERE s.unit_id=? AND e.occurred_at>=? AND e.source='event' ORDER BY e.id",(unit_id,cutoff)))
  else:events=[]
 for event in events:event["kind"]=event.get("characteristic") or "HomeKit event";event["note"]=event.get("value_json") or "Value changed";event["unit_id"]=unit_id
 return {"unit_id":unit_id,"source_id":source,"samples":samples,"transitions":transitions,"observations":observations+events}
def latest_for(unit_id):
 source=active_source(unit_id)
 with db() as conn:
  row=conn.execute("SELECT * FROM telemetry_samples WHERE unit_id=? AND source_id=? ORDER BY id DESC LIMIT 1",(unit_id,source)).fetchone() or conn.execute("SELECT * FROM telemetry_samples WHERE unit_id=? ORDER BY id DESC LIMIT 1",(unit_id,)).fetchone()
 return dict(row) if row else None
def units_status():
 with db() as conn:units=rows(conn.execute("SELECT * FROM units ORDER BY CASE id WHEN 't10' THEN 0 ELSE 1 END,display_name"));sources=rows(conn.execute("SELECT * FROM unit_sources WHERE enabled=1"));config=conn.execute("SELECT raw_json FROM auxiliary WHERE kind='configuration' ORDER BY id DESC LIMIT 1").fetchone()
 for unit in units:
  unit["sources"]=[s for s in sources if s["unit_id"]==unit["id"]];unit["latest"]=latest_for(unit["id"]);unit["active_source"]=unit["latest"]["source_id"] if unit["latest"] else active_source(unit["id"]);hk=HOMEKIT_MANAGER.status(unit["id"]) if HOMEKIT_MANAGER else {"ready":False,"paired":False};unit["homekit"]=hk;unit["resideo_connected"]=unit["id"]=="t10" and bool(get_kv("tokens"));unit["homekit_connected"]=hk.get("connected",False);unit["connected"]=unit["homekit_connected"] or unit["resideo_connected"];unit["last_poll"]=hk.get("accessories",[{}])[0].get("last_poll_at") if hk.get("accessories") else (get_kv("last_poll") if unit["id"]=="t10" else None);unit["poll_seconds"]=hk.get("poll_seconds") if hk.get("paired") else (POLL_SECONDS if unit["id"]=="t10" else None);unit["configuration"]=json.loads(config["raw_json"]) if config and unit["id"]=="t10" else None
 return units

def export_csv(unit_id):
 if unit_id!="all" and not unit_exists(unit_id):raise KeyError("Unknown thermostat unit")
 output=io.StringIO();writer=csv.writer(output);writer.writerow(["unit_id","unit_name","source_id","captured_at","indoor_temp","outdoor_temp","indoor_humidity","outdoor_humidity","cool_setpoint","heat_setpoint","system_mode","operation_mode","fan_request","is_alive"])
 with db() as conn:
  query="SELECT t.*,u.display_name FROM telemetry_samples t JOIN units u ON u.id=t.unit_id";args=()
  if unit_id!="all":query+=" WHERE t.unit_id=?";args=(unit_id,)
  for r in conn.execute(query+" ORDER BY t.id",args):writer.writerow([r["unit_id"],r["display_name"],r["source_id"],r["captured_at"],r["indoor_temp"],r["outdoor_temp"],r["indoor_humidity"],r["outdoor_humidity"],r["cool_setpoint"],r["heat_setpoint"],r["system_mode"],r["operation_mode"],r["fan_request"],r["is_alive"]])
 return output.getvalue().encode()

class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,fmt,*args):print(f"{self.log_date_time_string()} {fmt%args}")
 def send_json(self,value,status=200):
  body=json.dumps(value,separators=(",",":"),default=str).encode();self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(body)));self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(body)
 def redirect(self,target):self.send_response(302);self.send_header("Location",target);self.end_headers()
 def body_json(self):return json.loads(self.rfile.read(min(int(self.headers.get("Content-Length","0")),65536)) or b"{}")
 def do_GET(self):
  parsed=urllib.parse.urlparse(self.path);path=parsed.path;query=urllib.parse.parse_qs(parsed.query)
  try:
   hours=max(1,min(24*90,int(query.get("hours",["24"])[0])))
   if path=="/api/units":return self.send_json({"units":units_status()})
   if path.startswith("/api/units/") and path.endswith("/history"):
    unit_id=path.split("/")[3];return self.send_json(history_for(unit_id,hours))
   if path=="/api/compare":return self.send_json({"hours":hours,"units":[history_for("t10",hours),history_for("sensi",hours)]})
   if path=="/api/homekit/status":return self.send_json({**(HOMEKIT_MANAGER.status() if HOMEKIT_MANAGER else {"ready":False,"paired":False,"error":"HomeKit unavailable"}),"events":HOMEKIT_MANAGER.latest_events(30) if HOMEKIT_MANAGER else []})
   if path=="/api/homekit/discover":return self.send_json({"devices":HOMEKIT_MANAGER.discover()}) if HOMEKIT_MANAGER else self.send_json({"error":"HomeKit unavailable"},503)
   if path=="/api/homekit/history":return self.send_json(history_for(query.get("unit_id",["t10"])[0],hours))
   if path=="/auth/login":
    if not CLIENT_ID or not CLIENT_SECRET:return self.send_json({"error":"Set RESIDEO_CLIENT_ID and RESIDEO_CLIENT_SECRET"},400)
    state=secrets.token_urlsafe(24);put_kv("oauth_state_hash",hashlib.sha256(state.encode()).hexdigest());return self.redirect(API_BASE+"/oauth2/authorize?"+urllib.parse.urlencode({"response_type":"code","client_id":CLIENT_ID,"redirect_uri":REDIRECT_URI,"state":state}))
   if path=="/auth/callback":
    state=query.get("state",[""])[0]
    if not secrets.compare_digest(hashlib.sha256(state.encode()).hexdigest(),get_kv("oauth_state_hash","")):return self.send_json({"error":"Invalid OAuth state"},400)
    token=token_request({"grant_type":"authorization_code","code":query.get("code",[""])[0],"redirect_uri":REDIRECT_URI});token["expires_at"]=time.time()+int(token.get("expires_in",600));put_kv("tokens",token);discover();poll_once();return self.redirect("/")
   if path=="/api/status":
    unit=next(x for x in units_status() if x["id"]=="t10");return self.send_json({"connected":unit["connected"],"device":get_kv("selected_device"),"latest":unit["latest"],"configuration":unit["configuration"],"last_poll":get_kv("last_poll"),"poll_seconds":POLL_SECONDS})
   if path=="/api/history":return self.send_json(history_for("t10",hours))
   if path=="/api/export.csv":
    unit_id=query.get("unit_id",["t10"])[0];body=export_csv(unit_id);self.send_response(200);self.send_header("Content-Type","text/csv; charset=utf-8");self.send_header("Content-Disposition",f'attachment; filename="{unit_id}-thermostat-history.csv"');self.send_header("Content-Length",str(len(body)));self.end_headers();return self.wfile.write(body)
   if path=="/api/raw/latest":return self.send_json(json.loads((latest_for(query.get("unit_id",["t10"])[0]) or {}).get("raw_json","{}")))
   return self.static(path)
  except KeyError as exc:self.send_json({"error":str(exc).strip("'")},404)
  except urllib.error.HTTPError as exc:self.send_json({"error":f"Resideo API {exc.code}","detail":exc.read().decode(errors="replace")},502)
  except Exception as exc:self.send_json({"error":str(exc)},500)
 def do_POST(self):
  parsed=urllib.parse.urlparse(self.path);path=parsed.path
  try:
   if path=="/api/homekit/pair":
    if not HOMEKIT_MANAGER:return self.send_json({"error":"HomeKit unavailable"},503)
    body=self.body_json();return self.send_json(HOMEKIT_MANAGER.pair(str(body.get("device_id","")),str(body.get("code","")),body.get("unit_id")),201)
   if path.startswith("/api/units/") and path.endswith("/poll"):
    unit_id=path.split("/")[3]
    if not unit_exists(unit_id):return self.send_json({"error":"Unknown thermostat unit"},404)
    if homekit_paired(unit_id):return self.send_json(HOMEKIT_MANAGER.poll(unit_id))
    if unit_id=="t10":return self.send_json(poll_once())
    return self.send_json({"error":"Unit is not connected"},409)
   if path=="/api/poll":return self.send_json(poll_once())
   if path=="/api/observations":
    body=self.body_json();unit_id=body.get("unit_id") or None
    if unit_id and not unit_exists(unit_id):return self.send_json({"error":"Unknown thermostat unit"},404)
    kind=str(body.get("kind","note"))[:40];note=str(body.get("note",""))[:500];occurred=str(body.get("occurred_at") or now())
    with db() as conn:cur=conn.execute("INSERT INTO observations(occurred_at,kind,note,unit_id) VALUES(?,?,?,?)",(occurred,kind,note,unit_id))
    return self.send_json({"id":cur.lastrowid,"occurred_at":occurred,"unit_id":unit_id},201)
   return self.send_json({"error":"Not found"},404)
  except Exception as exc:self.send_json({"error":str(exc)},500)
 def static(self,path):
  relative="index.html" if path=="/" else path.lstrip("/");target=(STATIC/relative).resolve()
  if STATIC.resolve() not in target.parents or not target.is_file():return self.send_json({"error":"Not found"},404)
  mime={".html":"text/html",".css":"text/css",".js":"text/javascript",".svg":"image/svg+xml"}.get(target.suffix,"application/octet-stream");body=target.read_bytes();self.send_response(200);self.send_header("Content-Type",mime+"; charset=utf-8");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
if __name__=="__main__":
 init_db()
 try:
  from homekit_manager import HomeKitManager
  HOMEKIT_MANAGER=HomeKitManager(DATA);HOMEKIT_MANAGER.start()
 except Exception as exc:print(f"HomeKit disabled: {exc}")
 threading.Thread(target=collector,daemon=True).start();server=http.server.ThreadingHTTPServer((HOST,PORT),Handler);print(f"Thermostat Flight Recorder listening on http://{HOST}:{PORT}")
 try:server.serve_forever()
 except KeyboardInterrupt:print("\nStopped")
