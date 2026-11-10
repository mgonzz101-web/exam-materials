"""End-to-end tests against the real Flask app + a real (temporary) SQLite database.

Run:  python -m unittest discover -s tests -v
"""
import csv
import io
import os
import sys
import tempfile
import threading
import unittest
import uuid

TMP = tempfile.mkdtemp()
os.environ.update(
    SQLITE_PATH=os.path.join(TMP, "test.db"),
    SETUP_KEY="test-setup-key-123",
    MAIL_BACKEND="memory",
    MAIL_FROM="alerts@example.com",
    INSECURE_COOKIES="1",
    CHECKOUT_LIMIT_PER_MIN="100000",  # every test client shares 127.0.0.1
)
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402
import mailer  # noqa: E402

appmod.app.config["MAIL_SYNC"] = True  # send emails inline so tests can assert on them


class C:
    """Tiny API client that keeps its own cookies + CSRF token (like one browser)."""

    def __init__(self):
        self.c = appmod.app.test_client()
        self.csrf = self.c.get("/api/session").get_json()["csrf"]

    def _do(self, method, path, body=None, csrf=True):
        kw = {"headers": {"X-CSRF-Token": self.csrf} if csrf else {}}
        if body is not None:
            kw["json"] = body
        r = getattr(self.c, method)(path, **kw)
        j = r.get_json(silent=True) if r.is_json else None
        if isinstance(j, dict) and "csrf" in j:
            self.csrf = j["csrf"]
        return r.status_code, (j if r.is_json else r)

    def get(self, p):
        return self._do("get", p)

    def post(self, p, b=None, csrf=True):
        return self._do("post", p, {} if b is None else b, csrf)

    def put(self, p, b):
        return self._do("put", p, b)


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = C()
        body = {"name": "Olivia Owner", "email": "owner@example.com", "password": "correct horse battery"}
        s, _ = cls.owner.post("/api/setup", {**body, "setup_key": "WRONG"})
        assert s == 403, "wrong setup key must be rejected"
        s, j = cls.owner.post("/api/setup", {**body, "setup_key": "test-setup-key-123"})
        assert s == 200 and j["user"]["role"] == "owner", (s, j)
        s, _ = C().post("/api/setup", {**body, "email": "x@example.com", "setup_key": "test-setup-key-123"})
        assert s == 409, "a second owner can't be created through setup"
        s, j = cls.owner.put("/api/admin/settings", {"alert_recipients": "owner@example.com, buyer@example.com"})
        assert s == 200, j

    # ---- helpers
    def mk(self, qty, thr=5, exam=None, mt="record_form", form="", edition="2nd Ed"):
        exam = exam or "Exam-" + uuid.uuid4().hex[:6]
        s, j = self.owner.post("/api/admin/items", {
            "exam": exam, "edition": edition, "material_type": mt, "form_name": form,
            "quantity": qty, "low_threshold": thr, "location": "Shelf 1"})
        self.assertEqual(s, 201, j)
        return j["item"]["id"]

    def item(self, iid):
        s, j = self.owner.get("/api/admin/items")
        return next(i for i in j["items"] if i["id"] == iid)

    def qty(self, iid):
        return self.item(iid)["quantity"]

    def checkout(self, lines, token=None, name="Pat Teacher", email=None, client=None):
        client = client or C()
        return client.post("/api/checkout", {
            "token": token or uuid.uuid4().hex, "name": name,
            "email": email or f"pat-{uuid.uuid4().hex[:5]}@example.com",
            "lines": [{"item_id": i, "qty": q} for i, q in lines]})

    def movements(self, iid):
        s, j = self.owner.get(f"/api/admin/movements?item_id={iid}")
        return list(reversed(j["movements"]))  # oldest first

    def mails(self, text):
        return [m for m in mailer.OUTBOX if text in m["text"]]

    # ---- permissions
    def test_permissions_enforced_on_server(self):
        anon = C()
        for path in ("/api/admin/items", "/api/admin/dashboard", "/api/admin/checkouts", "/api/admin/settings",
                     "/api/admin/export/inventory.csv", "/api/admin/movements"):
            self.assertEqual(anon.get(path)[0], 401, path)
        iid = self.mk(10)
        s, j = anon.post("/api/register", {"name": "Regular User", "email": "u1@example.com", "password": "password123"})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["user"]["role"], "user")
        for method, path, body in [
            ("get", "/api/admin/items", None),
            ("post", "/api/admin/items", {"exam": "X", "material_type": "record_form"}),
            ("post", f"/api/admin/items/{iid}/adjust", {"mode": "set", "amount": 999, "reason": "hack"}),
            ("put", "/api/admin/settings", {"alert_recipients": "evil@example.com"}),
            ("get", "/api/admin/export/transactions.csv", None),
            ("post", "/api/admin/users", {"name": "Evil", "email": "e@example.com", "role": "owner", "password": "passwordpassword"}),
        ]:
            s, _ = anon._do(method, path, body)
            self.assertEqual(s, 403, path)
        self.assertEqual(self.qty(iid), 10)
        # user CAN browse + check out
        self.assertEqual(self.checkout([(iid, 1)], client=anon)[0], 200)

    def test_csrf_required(self):
        c = C()
        s, _ = c.post("/api/checkout", {"token": uuid.uuid4().hex, "name": "A", "email": "a@b.co", "lines": []}, csrf=False)
        self.assertEqual(s, 403)

    # ---- checkout
    def test_cart_browsing_never_deducts_and_multi_item_checkout_deducts(self):
        a, b, c = self.mk(10, exam="Multi-A"), self.mk(8, exam="Multi-B", mt="response_booklet", form="Booklet 1"), self.mk(3, exam="Multi-C")
        for _ in range(3):
            s, j = C().get("/api/items")
            self.assertEqual(s, 200)
        self.assertEqual((self.qty(a), self.qty(b), self.qty(c)), (10, 8, 3))
        s, j = self.checkout([(a, 2), (b, 3), (c, 1)], name="Casey Counselor", email="casey@example.com")
        self.assertEqual(s, 200, j)
        r = j["receipt"]
        self.assertEqual(r["total_units"], 6)
        self.assertEqual({l["item_id"]: l["qty"] for l in r["lines"]}, {a: 2, b: 3, c: 1})
        self.assertEqual((self.qty(a), self.qty(b), self.qty(c)), (8, 5, 2))
        mv = self.movements(b)
        self.assertEqual((mv[-1]["kind"], mv[-1]["delta"], mv[-1]["prev_qty"], mv[-1]["new_qty"], mv[-1]["actor_name"]),
                         ("checkout", -3, 8, 5, "Casey Counselor"))
        s, hist = self.owner.get("/api/admin/checkouts?q=casey@example.com")
        self.assertEqual(len(hist["checkouts"]), 1)
        self.assertEqual(len(hist["checkouts"][0]["lines"]), 3)

    def test_checkout_is_all_or_nothing_and_explains_problem(self):
        a, b = self.mk(5), self.mk(1)
        email = f"aon-{uuid.uuid4().hex[:5]}@example.com"
        s, j = self.checkout([(a, 2), (b, 2)], email=email)
        self.assertEqual(s, 409)
        self.assertEqual([p["item_id"] for p in j["problems"]], [b])
        self.assertEqual(j["problems"][0]["available"], 1)
        self.assertEqual((self.qty(a), self.qty(b)), (5, 1), "nothing may be deducted")
        self.assertEqual(self.owner.get(f"/api/admin/checkouts?q={email}")[1]["checkouts"], [])
        # user fixes the cart and succeeds
        self.assertEqual(self.checkout([(a, 2), (b, 1)], email=email)[0], 200)
        self.assertEqual((self.qty(a), self.qty(b)), (3, 0))
        # out-of-stock item is reported as such
        s, j = self.checkout([(b, 1)])
        self.assertEqual((s, j["problems"][0]["reason"]), (409, "out_of_stock"))

    def test_invalid_quantities_rejected(self):
        a = self.mk(10)
        for bad in (0, -1, -5, 1.5, "abc", None, 10_001):
            s, _ = self.checkout([(a, bad)])
            self.assertEqual(s, 400, bad)
        self.assertEqual(self.checkout([])[0], 400)
        self.assertEqual(C().post("/api/checkout", {"token": "short", "name": "A", "email": "a@b.co", "lines": [{"item_id": a, "qty": 1}]})[0], 400)
        self.assertEqual(self.checkout([(a, 1)], email="not-an-email")[0], 400)
        self.assertEqual(self.checkout([(a, 1)], name="")[0], 400)
        self.assertEqual(self.qty(a), 10)

    def test_repeated_submit_with_same_token_deducts_once(self):
        a = self.mk(10)
        tok = uuid.uuid4().hex
        s1, j1 = self.checkout([(a, 4)], token=tok)
        s2, j2 = self.checkout([(a, 4)], token=tok)
        s3, j3 = self.checkout([(a, 4)], token=tok)
        self.assertEqual((s1, s2, s3), (200, 200, 200))
        self.assertFalse(j1["duplicate"])
        self.assertTrue(j2["duplicate"] and j3["duplicate"])
        self.assertEqual(j1["receipt"]["id"], j2["receipt"]["id"])
        self.assertEqual(self.qty(a), 6)
        # refreshing the confirmation page just re-reads the receipt
        s, j = C().get("/api/receipt/" + tok)
        self.assertEqual((s, j["receipt"]["total_units"]), (200, 4))
        self.assertEqual(self.qty(a), 6)

    def test_simultaneous_checkouts_never_oversell(self):
        a = self.mk(10, thr=0)
        results, barrier = [], threading.Barrier(8)

        def go():
            c = C()
            barrier.wait()
            results.append(self.checkout([(a, 2)], client=c)[0])

        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), [200] * 5 + [409] * 3, results)
        self.assertEqual(self.qty(a), 0)
        self.assertTrue(all(m["new_qty"] >= 0 for m in self.movements(a)))

    def test_simultaneous_double_click_same_token(self):
        a = self.mk(10)
        tok, results, barrier = uuid.uuid4().hex, [], threading.Barrier(6)

        def go():
            c = C()
            barrier.wait()
            results.append(self.checkout([(a, 3)], token=tok, client=c)[0])

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results, [200] * 6)
        self.assertEqual(self.qty(a), 7, "six simultaneous taps must deduct exactly once")

    # ---- manual adjustments + audit
    def test_manual_adjustments_saved_and_audited(self):
        a = self.mk(20, thr=5)
        o = self.owner
        self.assertEqual(o.post(f"/api/admin/items/{a}/adjust", {"mode": "add", "amount": 5})[0], 400, "reason required")
        self.assertEqual(o.post(f"/api/admin/items/{a}/adjust", {"mode": "add", "amount": -5, "reason": "x"})[0], 400)
        self.assertEqual(o.post(f"/api/admin/items/{a}/adjust", {"mode": "remove", "amount": 21, "reason": "x"})[0], 409)
        s, j = o.post(f"/api/admin/items/{a}/adjust", {"mode": "add", "amount": 30, "reason": "Shipment arrived"})
        self.assertEqual((s, j["item"]["quantity"]), (200, 50))
        s, j = o.post(f"/api/admin/items/{a}/adjust", {"mode": "remove", "amount": 4, "reason": "Water damage"})
        self.assertEqual(j["item"]["quantity"], 46)
        s, j = o.post(f"/api/admin/items/{a}/adjust", {"mode": "set", "amount": 41, "reason": "Physical count"})
        self.assertEqual(j["item"]["quantity"], 41)
        self.assertEqual(self.qty(a), 41, "adjustments persist")
        mv = self.movements(a)
        self.assertEqual([(m["kind"], m["delta"], m["prev_qty"], m["new_qty"], m["reason"], m["actor_name"]) for m in mv], [
            ("initial", 20, 0, 20, "Item created", "Olivia Owner"),
            ("restock", 30, 20, 50, "Shipment arrived", "Olivia Owner"),
            ("removed", -4, 50, 46, "Water damage", "Olivia Owner"),
            ("count", -5, 46, 41, "Physical count", "Olivia Owner")])

    def test_reverse_checkout_restores_exactly_once(self):
        a, b = self.mk(10), self.mk(10)
        email = f"rev-{uuid.uuid4().hex[:5]}@example.com"
        s, j = self.checkout([(a, 4), (b, 2)], email=email)
        cid = j["receipt"]["id"]
        self.assertEqual((self.qty(a), self.qty(b)), (6, 8))
        self.assertEqual(self.owner.post(f"/api/admin/checkouts/{cid}/reverse", {})[0], 400, "reason required")
        out, barrier = [], threading.Barrier(5)

        def go():
            oc = C()
            oc.post("/api/login", {"email": "owner@example.com", "password": "correct horse battery"})
            barrier.wait()
            out.append(oc.post(f"/api/admin/checkouts/{cid}/reverse", {"reason": "Taken by mistake"})[0])

        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(out), [200, 409, 409, 409, 409])
        self.assertEqual((self.qty(a), self.qty(b)), (10, 10), "restored once, not five times")
        self.assertEqual(self.owner.post(f"/api/admin/checkouts/{cid}/reverse", {"reason": "again"})[0], 409)
        self.assertEqual(self.qty(a), 10)
        self.assertEqual([m["kind"] for m in self.movements(a)], ["initial", "checkout", "checkout_reversal"])
        h = self.owner.get(f"/api/admin/checkouts?q={email}")[1]["checkouts"][0]
        self.assertEqual((h["status"], h["reverse_reason"]), ("reversed", "Taken by mistake"))

    def test_correct_checkout(self):
        a = self.mk(10)
        s, j = self.checkout([(a, 5)])
        cid, line = j["receipt"]["id"], j["receipt"]["lines"][0]["id"]
        s, j = self.owner.post(f"/api/admin/checkouts/{cid}/correct", {"lines": [{"line_id": line, "qty": 3}], "reason": "Took 3"})
        self.assertEqual(s, 200, j)
        self.assertEqual(self.qty(a), 7)
        s, j = self.owner.post(f"/api/admin/checkouts/{cid}/correct", {"lines": [{"line_id": line, "qty": 4}], "reason": "Actually 4"})
        self.assertEqual(self.qty(a), 6)
        self.assertEqual(self.owner.post(f"/api/admin/checkouts/{cid}/correct", {"lines": [{"line_id": line, "qty": 99}], "reason": "x"})[0], 409)
        self.assertEqual(self.owner.post(f"/api/admin/checkouts/{cid}/correct", {"lines": [{"line_id": line, "qty": 0}], "reason": "x"})[0], 400)
        self.assertEqual(self.qty(a), 6)

    def test_archive_hides_from_checkout(self):
        a = self.mk(10, exam="Archivable")
        self.assertIn(a, [i["id"] for i in C().get("/api/items")[1]["items"]])
        self.assertEqual(self.owner.post(f"/api/admin/items/{a}/archive", {"archived": True})[0], 200)
        self.assertNotIn(a, [i["id"] for i in C().get("/api/items")[1]["items"]])
        s, j = self.checkout([(a, 1)])
        self.assertEqual(s, 409)
        self.owner.post(f"/api/admin/items/{a}/archive", {"archived": False})
        self.assertEqual(self.checkout([(a, 1)])[0], 200)

    def test_duplicate_item_rejected(self):
        self.mk(1, exam="DupExam", mt="response_booklet", form="B1", edition="1st")
        s, _ = self.owner.post("/api/admin/items", {"exam": "dupexam", "edition": "1ST", "material_type": "response_booklet", "form_name": "b1", "quantity": 1})
        self.assertEqual(s, 409)
        # same exam, different material type is a separate item with its own quantity
        s, _ = self.owner.post("/api/admin/items", {"exam": "DupExam", "edition": "1st", "material_type": "record_form", "form_name": "B1", "quantity": 7})
        self.assertEqual(s, 201)

    # ---- low-stock alerts
    def test_low_stock_alert_fires_once_then_rearms_after_restock(self):
        a = self.mk(12, thr=10, exam="AlertExam-1", mt="response_booklet", form="Booklet Z")
        base = len(self.mails("AlertExam-1"))
        self.checkout([(a, 1)])  # 11 — still above
        self.assertEqual(len(self.mails("AlertExam-1")), base)
        self.checkout([(a, 1)])  # 10 — reaches threshold => alert
        self.assertEqual(len(self.mails("AlertExam-1")), base + 1)
        m = self.mails("AlertExam-1")[-1]
        self.assertEqual(sorted(m["to"]), ["buyer@example.com", "owner@example.com"])
        self.assertIn("Booklet Z", m["text"])
        self.assertIn("10 left", m["text"])
        self.checkout([(a, 1)])  # 9
        self.checkout([(a, 2)])  # 7
        self.assertEqual(len(self.mails("AlertExam-1")), base + 1, "no repeat alerts while still low")
        self.assertTrue(self.item(a)["alert_active"])
        s, dash = self.owner.get("/api/admin/dashboard")
        row = next(i for i in dash["needs_reordering"] if i["id"] == a)
        self.assertEqual((row["quantity"], row["low_threshold"], row["status"]), (7, 10, "low"))
        self.owner.post(f"/api/admin/items/{a}/adjust", {"mode": "add", "amount": 20, "reason": "Restock"})  # 27 > 10 resets
        self.assertFalse(self.item(a)["alert_active"])
        self.assertNotIn(a, [i["id"] for i in self.owner.get("/api/admin/dashboard")[1]["needs_reordering"]])
        self.assertEqual(len(self.mails("AlertExam-1")), base + 1)
        self.checkout([(a, 20)])  # 7 again => new alert
        self.assertEqual(len(self.mails("AlertExam-1")), base + 2)

    def test_alert_after_manual_decrease_and_count(self):
        a = self.mk(20, thr=10, exam="AlertExam-2")
        base = len(self.mails("AlertExam-2"))
        self.owner.post(f"/api/admin/items/{a}/adjust", {"mode": "remove", "amount": 12, "reason": "Discarded"})  # 8
        self.assertEqual(len(self.mails("AlertExam-2")), base + 1)
        self.owner.post(f"/api/admin/items/{a}/adjust", {"mode": "set", "amount": 50, "reason": "Count"})
        self.owner.post(f"/api/admin/items/{a}/adjust", {"mode": "set", "amount": 3, "reason": "Recount"})
        self.assertEqual(len(self.mails("AlertExam-2")), base + 2)

    def test_one_email_covers_several_items_in_one_checkout(self):
        a, b = self.mk(6, thr=5, exam="Combo-A"), self.mk(6, thr=5, exam="Combo-B")
        before = len(mailer.OUTBOX)
        self.checkout([(a, 1), (b, 2)])
        new = mailer.OUTBOX[before:]
        self.assertEqual(len(new), 1)
        self.assertIn("Combo-A", new[0]["text"])
        self.assertIn("Combo-B", new[0]["text"])

    def test_changing_threshold_evaluates_alert(self):
        a = self.mk(8, thr=5, exam="AlertExam-3")
        base = len(self.mails("AlertExam-3"))
        s, j = self.owner.put(f"/api/admin/items/{a}", {"exam": "AlertExam-3", "edition": "2nd Ed", "material_type": "record_form", "form_name": "", "low_threshold": 10, "location": "", "notes": ""})
        self.assertEqual(s, 200, j)
        self.assertEqual(len(self.mails("AlertExam-3")), base + 1)
        self.assertEqual(j["item"]["status"], "low")

    def test_email_failure_never_breaks_checkout(self):
        a = self.mk(6, thr=5, exam="AlertExam-4")
        orig = mailer.send
        mailer.send = lambda *a, **k: (False, "boom")
        try:
            s, j = self.checkout([(a, 2)])
        finally:
            mailer.send = orig
        self.assertEqual(s, 200)
        self.assertEqual(self.qty(a), 4)
        alerts = self.owner.get("/api/admin/settings")[1]["alerts"]
        self.assertEqual(next(x for x in alerts if "AlertExam-4" in x["label"])["status"], "failed")

    def test_no_recipients_is_recorded_not_crashing(self):
        s, _ = self.owner.put("/api/admin/settings", {"alert_recipients": ""})
        try:
            a = self.mk(6, thr=5, exam="AlertExam-5")
            before = len(mailer.OUTBOX)
            self.assertEqual(self.checkout([(a, 2)])[0], 200)
            self.assertEqual(len(mailer.OUTBOX), before)
            al = next(x for x in self.owner.get("/api/admin/settings")[1]["alerts"] if "AlertExam-5" in x["label"])
            self.assertEqual(al["status"], "not_configured")
            self.assertEqual(self.item(a)["status"], "low")
        finally:
            self.owner.put("/api/admin/settings", {"alert_recipients": "owner@example.com, buyer@example.com"})

    # ---- accounts / settings
    def test_guest_checkout_can_be_disabled(self):
        a = self.mk(10)
        self.owner.put("/api/admin/settings", {"allow_guest": False})
        try:
            self.assertEqual(self.checkout([(a, 1)])[0], 401)
            u = C()
            u.post("/api/register", {"name": "Signed In", "email": f"si-{uuid.uuid4().hex[:5]}@example.com", "password": "password123"})
            s, j = u.post("/api/checkout", {"token": uuid.uuid4().hex, "lines": [{"item_id": a, "qty": 1}]})
            self.assertEqual(s, 200, "signed-in profile supplies name/email")
            self.assertEqual(j["receipt"]["name"], "Signed In")
        finally:
            self.owner.put("/api/admin/settings", {"allow_guest": True})

    def test_login_throttling(self):
        c = C()
        codes = [c.post("/api/login", {"email": "nobody@example.com", "password": "nope"})[0] for _ in range(10)]
        self.assertEqual(codes[0], 401)
        self.assertEqual(codes[-1], 429)

    def test_inactive_user_loses_access_immediately(self):
        s, _ = self.owner.post("/api/admin/users", {"name": "Second Owner", "email": "o2@example.com", "role": "owner", "password": "another long password"})
        self.assertEqual(s, 201)
        o2 = C()
        self.assertEqual(o2.post("/api/login", {"email": "o2@example.com", "password": "another long password"})[0], 200)
        self.assertEqual(o2.get("/api/admin/items")[0], 200)
        uid = next(u["id"] for u in self.owner.get("/api/admin/users")[1]["users"] if u["email"] == "o2@example.com")
        self.owner.post(f"/api/admin/users/{uid}/active", {"active": False})
        self.assertEqual(o2.get("/api/admin/items")[0], 401)

    # ---- exports
    def test_csv_exports(self):
        a = self.mk(10, exam="CsvExam", form="F1")
        self.checkout([(a, 3)], name="=HYPERLINK(\"http://evil\")", email="csv@example.com")
        s, r = self.owner.get("/api/admin/export/inventory.csv")
        rows = list(csv.reader(io.StringIO(r.data.decode("utf-8-sig"))))
        self.assertEqual(rows[0][:5], ["Exam", "Edition/Version", "Material type", "Form/Booklet name", "Quantity"])
        mine = next(x for x in rows if x[0] == "CsvExam")
        self.assertEqual((mine[2], mine[3], mine[4], mine[6]), ("Record Form", "F1", "7", "In Stock"))
        s, r = self.owner.get("/api/admin/export/transactions.csv")
        txt = r.data.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(txt)))
        mine = next(x for x in rows if x[4] == "CsvExam")
        self.assertTrue(mine[2].startswith("'="), "formula injection neutralised")
        self.assertEqual(mine[8], "3")
        s, r = self.owner.get("/api/admin/export/audit.csv")
        rows = list(csv.reader(io.StringIO(r.data.decode("utf-8-sig"))))
        self.assertTrue(any(x[1] == "CsvExam" and x[5] == "-3" and x[6] == "10" and x[7] == "7" for x in rows))

    def test_history_filters(self):
        a = self.mk(10, exam="FilterExam")
        self.checkout([(a, 1)], email="filter-me@example.com")
        j = self.owner.get("/api/admin/checkouts?exam=filterexam")[1]
        self.assertEqual(len(j["checkouts"]), 1)
        self.assertEqual(self.owner.get("/api/admin/checkouts?exam=filterexam&from=2999-01-01")[1]["checkouts"], [])
        self.assertEqual(len(self.owner.get("/api/admin/checkouts?q=filter-me&status=completed")[1]["checkouts"]), 1)

    def test_qr_download_is_scannable_and_owner_only(self):
        self.assertEqual(C().get("/api/admin/qr.png")[0], 401)
        self.owner.put("/api/admin/settings", {"public_url": "https://materials.example.org"})
        try:
            s, r = self.owner.get("/api/admin/qr.png?download=1")
            self.assertEqual(s, 200)
            self.assertIn("attachment", r.headers["Content-Disposition"])
            try:
                import cv2
                import numpy as np
            except ImportError:
                return
            img = cv2.imdecode(np.frombuffer(r.data, np.uint8), cv2.IMREAD_GRAYSCALE)
            self.assertEqual(cv2.QRCodeDetector().detectAndDecode(img)[0], "https://materials.example.org/")
            s, svg = self.owner.get("/api/admin/qr.svg")
            self.assertEqual(s, 200)
            self.assertTrue(svg.data.startswith(b"<svg"))
        finally:
            self.owner.put("/api/admin/settings", {"public_url": ""})

    def test_pages_and_headers(self):
        c = C()
        for p in ("/", "/admin", "/setup", "/static/shop.js", "/healthz"):
            r = c.c.get(p)
            self.assertEqual(r.status_code, 200, p)
        r = c.c.get("/")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
