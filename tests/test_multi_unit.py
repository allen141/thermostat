import asyncio,json,sqlite3,tempfile,unittest
from pathlib import Path
import server
from homekit_manager import HomeKitManager

class MultiUnitTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.old=server.DB_PATH;server.DB_PATH=Path(self.temp.name)/"thermostat.sqlite";server.HOMEKIT_MANAGER=None;server.init_db()
 def tearDown(self):server.DB_PATH=self.old;self.temp.cleanup()
 def test_migration_is_idempotent_and_preserves_legacy_sample(self):
  with server.db() as conn:
   sample_id=conn.execute("INSERT INTO samples(captured_at,device_id,indoor_temp,raw_json) VALUES(?,?,?,?)",(server.now(),"old-t10",72.5,json.dumps({"old":True}))).lastrowid
   conn.execute("INSERT INTO transitions(occurred_at,field,from_value,to_value,sample_id) VALUES(?,?,?,?,?)",(server.now(),"operation_mode","Off","Cooling",sample_id))
  server.init_db();server.init_db()
  with server.db() as conn:
   rows=conn.execute("SELECT unit_id,source_id,indoor_temp,raw_json FROM telemetry_samples").fetchall()
   transition_count=conn.execute("SELECT count(*) FROM unit_transitions WHERE unit_id='t10'").fetchone()[0]
  self.assertEqual(1,len(rows));self.assertEqual(("t10","t10-resideo",72.5),tuple(rows[0][:3]));self.assertEqual({"old":True},json.loads(rows[0]["raw_json"]));self.assertEqual(1,transition_count)
 def test_histories_and_observations_are_scoped(self):
  stamp=server.now()
  with server.db() as conn:
   conn.execute("INSERT OR IGNORE INTO unit_sources VALUES('sensi-homekit','sensi','homekit','sensi-test',100,1,?)",(stamp,))
   for uid,sid,temp in (("t10","t10-resideo",71),("sensi","sensi-homekit",75)):conn.execute("INSERT INTO telemetry_samples(captured_at,unit_id,source_id,indoor_temp,raw_json) VALUES(?,?,?,?,?)",(stamp,uid,sid,temp,"{}"))
   conn.execute("INSERT INTO observations(occurred_at,kind,note,unit_id) VALUES(?,?,?,?)",(stamp,"t10_only","","t10"));conn.execute("INSERT INTO observations(occurred_at,kind,note,unit_id) VALUES(?,?,?,NULL)",(stamp,"house",""))
  t10=server.history_for("t10",24);sensi=server.history_for("sensi",24)
  self.assertEqual([71], [x["indoor_temp"] for x in t10["samples"]]);self.assertEqual([75],[x["indoor_temp"] for x in sensi["samples"]]);self.assertEqual({"t10_only","house"},{x["kind"] for x in t10["observations"]});self.assertEqual({"house"},{x["kind"] for x in sensi["observations"]})
 def test_homekit_characteristics_are_namespaced_by_alias(self):
  manager=HomeKitManager(Path(self.temp.name));manager.characteristics={("t10-alias",1,10):{"service_type":"0000004A","type":"00000011","value":20},("sensi-alias",1,10):{"service_type":"0000004A","type":"00000011","value":25}}
  self.assertEqual(20,manager._values("t10-alias")["current_temp_c"]);self.assertEqual(25,manager._values("sensi-alias")["current_temp_c"])
 def test_homekit_supervisor_retries_after_startup_failure(self):
  async def scenario():
   manager=HomeKitManager(Path(self.temp.name));manager.reconnect_seconds=.01;attempts=[]
   async def setup(alias,pairing):
    attempts.append(alias)
    if len(attempts)==1:raise OSError("thermostat offline")
    manager.poll_state.setdefault(alias,{}).update(connection_state="connected",connection_error=None)
   async def healthcheck(alias,pairing,store_sample=True):
    manager.poll_state.setdefault(alias,{}).update(connection_state="connected",connection_error=None)
   manager._setup_pairing=setup;manager._poll_pairing=healthcheck
   task=asyncio.create_task(manager._supervise_pairing("sensi-test",object()))
   await asyncio.sleep(.04);task.cancel()
   try:await task
   except asyncio.CancelledError:pass
   self.assertGreaterEqual(len(attempts),2);self.assertEqual("connected",manager.poll_state["sensi-test"]["connection_state"]);self.assertIsNone(manager.poll_state["sensi-test"]["connection_error"])
  asyncio.run(scenario())
 def test_homekit_second_sample_uses_named_rows(self):
  manager=HomeKitManager(Path(self.temp.name));manager._ensure_source("resideo-t10","t10","T10","T10")
  service="0000004A";manager.characteristics={("resideo-t10",1,1):{"service_type":service,"type":"00000011","value":20},("resideo-t10",1,2):{"service_type":service,"type":"00000010","value":51},("resideo-t10",1,3):{"service_type":service,"type":"00000035","value":22},("resideo-t10",1,4):{"service_type":service,"type":"0000000F","value":0},("resideo-t10",1,5):{"service_type":service,"type":"00000033","value":2}}
  manager._store_sample("resideo-t10","snapshot");manager.characteristics[("resideo-t10",1,4)]["value"]=2;manager._store_sample("resideo-t10","event")
  with server.db() as conn:
   self.assertEqual(2,conn.execute("SELECT COUNT(*) FROM telemetry_samples WHERE source_id='t10-homekit'").fetchone()[0]);self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM unit_transitions WHERE unit_id='t10'").fetchone()[0],1)

 def test_chart_keeps_humidity_scaling_and_separate_equipment_lanes(self):
  source=(Path(__file__).parents[1]/"static"/"app.js").read_text()
  self.assertIn("function humidityDomain",source);self.assertNotIn("hy=v=>p.b-v/100",source);self.assertIn("humidity.hi-(humidity.hi-humidity.lo)",source)
  self.assertIn("`${unitName} cool`",source);self.assertIn("`${unitName} fan`",source);self.assertIn("Number(r.fan_request)===1",source)

 def test_unknown_unit_is_rejected(self):
  with self.assertRaises(KeyError):server.history_for("garage",24)

if __name__=="__main__":unittest.main()
