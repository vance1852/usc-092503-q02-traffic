import unittest
from penalty_ops.models import ViolationRecord,CaseRecord
from penalty_ops.risk import score_violation_record
from penalty_ops.service import PenaltyService
class UrbanEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.s=PenaltyService(); self.s.bootstrap(); self.t=self.s.auth.login("admin","enforcement-admin"); self.s.register_case_record(self.t,CaseRecord("S1","east","drainage",100,4))
    def test_risk_and_idempotent_violation_record(self):
        r=ViolationRecord("R1","S1","evidence_source",120,250,90,"2026-01-01T00:00:00+00:00"); a=self.s.ingest_violation_record(self.t,r); b=self.s.ingest_violation_record(self.t,r); self.assertFalse(a["duplicate"]); self.assertTrue(b["duplicate"]); self.assertEqual(self.s.risk_report(self.t,"S1")["violation_records"],1)
    def test_case_ticket_and_allocation(self):
        r=self.s.ingest_violation_record(self.t,ViolationRecord("R2","S1","evidence_source",100,250,90,"2026-01-01T00:00:00+00:00")); o=self.s.create_case_ticket(self.t,"S1",r["alert_id"],"crew"); self.s.transition_case_ticket(self.t,o["case_ticket_id"],"assigned","crew accepted"); self.s.add_response_resource(self.t,"R1","pump","east",2); self.assertFalse(self.s.allocate(self.t,"R1",o["case_ticket_id"],1)["duplicate"]); self.assertEqual(self.s.response_resource(self.t,"R1")["available"],1)
    def test_risk_validation(self):
        with self.assertRaises(ValueError):score_violation_record(-1,1,1,2)
