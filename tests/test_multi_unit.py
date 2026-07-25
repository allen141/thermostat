import json,sqlite3,tempfile,unittest
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
 def test_unknown_unit_is_rejected(self):
  with self.assertRaises(KeyError):server.history_for("garage",24)

if __name__=="__main__":unittest.main()
