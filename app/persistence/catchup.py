"""Durable observation gaps and leased evidence checks."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4


@dataclass
class CatchupPolicy:
    catchup_enabled: bool = True
    catchup_canary_interval_minutes: int = 60
    catchup_daily_page_cap: int = 1000
    catchup_grant_min_interval_minutes: int = 10
    catchup_lease_seconds: int = 300
    catchup_grant_page_chunk: int = 200
    catchup_skew_margin_minutes: int = 15
    capture_report_max_age_seconds: int = 90
    agent_lease_timeout_seconds: int = 60


class CatchupEvidenceError(ValueError):
    pass


PAUSED = {
    "paused": "user_paused", "consent_needed": "consent_needed",
    "capture_off": "capture_off", "account_mismatch": "account_changed",
    "storage_locked": "applying_settings", "no_onlyfans_tab": "no_onlyfans_tab",
    "tab_frozen": "onlyfans_sleeping", "tab_discarded": "onlyfans_sleeping",
}
COUNTERS = ("canary_list", "catchup_list", "catchup_messages", "history_list",
            "history_messages", "identity", "retries", "expired", "rejected")


def instant(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def earlier(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return min((left, right), key=instant)


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CatchupLedger:
    def __init__(self, database, policy=None):
        self.database = database
        self.policy = policy or CatchupPolicy()

    @staticmethod
    def _baseline(c, account):
        row = c.execute(
            """SELECT MAX(g.closed_at) FROM coverage_generations g
               LEFT JOIN account_coverage_heads h USING(creator_account_id)
               WHERE g.creator_account_id=? AND
                 (g.state='complete' OR g.generation_id=h.last_complete_generation_id)""",
            (account,),
        ).fetchone()
        return row[0]

    def _load(self, c, account):
        baseline = self._baseline(c, account)
        c.execute("""INSERT OR IGNORE INTO message_catchup
            (creator_account_id,uncertain_since,last_closed_at) VALUES (?,?,?)""",
            (account, baseline, baseline))
        r = dict(c.execute("SELECT * FROM message_catchup WHERE creator_account_id=?", (account,)).fetchone())
        if r["last_closed_at"] is None and baseline is not None:
            r["last_closed_at"] = baseline
            r["uncertain_since"] = earlier(r["uncertain_since"], baseline)
        return r

    @staticmethod
    def _save(c, r):
        columns = [name for name in r if name != "creator_account_id"]
        c.execute("UPDATE message_catchup SET " + ",".join(f"{name}=?" for name in columns)
                  + " WHERE creator_account_id=?",
                  (*[r[name] for name in columns], r["creator_account_id"]))

    @staticmethod
    def _active(c, r):
        row = c.execute("SELECT * FROM message_checks WHERE check_id=? AND creator_account_id=? AND state IN ('active','suspended')",
                        (r["active_check_id"], r["creator_account_id"])).fetchone()
        return None if row is None else dict(row)

    def _trigger(self, c, r, boundary):
        check = self._active(c, r)
        if not r["gap_open"]:
            r.update(gap_open=1, gap_epoch=r["gap_epoch"] + 1, uncertain_since=boundary)
        elif check is not None:
            if check["epoch"] == r["gap_epoch"]:
                r["gap_epoch"] += 1
            r["pending_uncertain_since"] = earlier(r["pending_uncertain_since"], boundary)
        else:
            r["uncertain_since"] = earlier(r["uncertain_since"], boundary)

    def _live(self, r, now):
        last = r["last_heartbeat_at"] if r["session_present"] else r["disconnected_at"]
        return last is not None and (now - instant(last)).total_seconds() <= self.policy.agent_lease_timeout_seconds

    def _tick(self, c, r, now):
        live = self._live(r, now)
        if not live and r["last_heartbeat_at"] is not None and not r["loss_recorded"]:
            self._trigger(c, r, r["last_heartbeat_at"])
            r["loss_recorded"] = 1
        report_fresh = r["last_report_at"] is not None and (now - instant(r["last_report_at"])).total_seconds() <= self.policy.capture_report_max_age_seconds
        observing = bool(live and r["reported_observing"] and report_fresh)
        if r["observing"] and not observing:
            if not r["reported_observing"] or not report_fresh:
                self._trigger(c, r, r["last_observing_report_at"])
            r["observing_since"] = None
        if observing and not r["observing"]:
            r["observing_since"] = now.isoformat()
            r["blind_catchup_done"] = 0
        r["observing"] = int(observing)
        check = self._active(c, r)
        if check is not None and check["state"] == "suspended" and check["suspended_day"] < now.date().isoformat():
            midnight = datetime.combine(instant(check["suspended_day"]).date() + timedelta(days=1), datetime.min.time(), timezone.utc)
            check.update(state="active", lease_expires_at=(midnight + timedelta(seconds=self.policy.catchup_lease_seconds)).isoformat())
            c.execute("UPDATE message_checks SET state='active',suspended_day=NULL,lease_expires_at=? WHERE check_id=?", (check["lease_expires_at"], check["check_id"]))
        if check is not None and check["state"] == "active" and now >= instant(check["lease_expires_at"]):
            c.execute("UPDATE message_checks SET state='abandoned',reason='lease_expired' WHERE check_id=?", (check["check_id"],))
            r["active_check_id"] = None
            r["incomplete"] = 1

    def admit(self, account, *, installation, stream, supported, now):
        with self.database.transaction() as c:
            r = self._load(c, account)
            self._tick(c, r, now)
            r.update(session_present=1, disconnected_at=None, last_heartbeat_at=now.isoformat(),
                     agent_installation_id=installation, agent_stream_id=stream, supported=int(supported), loss_recorded=0)
            self._tick(c, r, now)
            self._save(c, r)

    def heartbeat(self, account, *, now):
        with self.database.transaction() as c:
            r = self._load(c, account)
            self._tick(c, r, now)
            r.update(last_heartbeat_at=now.isoformat(), session_present=1, disconnected_at=None, loss_recorded=0)
            self._tick(c, r, now)
            self._save(c, r)

    def disconnect(self, account, *, now):
        with self.database.transaction() as c:
            r = self._load(c, account)
            if r["session_present"]:
                r.update(session_present=0, disconnected_at=now.isoformat())
            self._save(c, r)

    def epoch_changed(self, account, *, now, connection=None):
        def apply(c):
            r = self._load(c, account)
            self._trigger(c, r, now.isoformat())
            self._save(c, r)
        if connection is not None:
            apply(connection)
        else:
            with self.database.transaction() as c:
                apply(c)

    def restart(self, *, now):
        with self.database.transaction() as c:
            for account, in c.execute("SELECT creator_account_id FROM message_catchup").fetchall():
                r = self._load(c, account)
                self._trigger(c, r, r["last_report_at"] or r["uncertain_since"] or now.isoformat())
                r.update(session_present=0, disconnected_at=None, observing=0, observing_since=None, reported_observing=0, loss_recorded=1)
                self._save(c, r)

    def expire(self, *, now):
        with self.database.transaction() as c:
            for account, in c.execute("SELECT creator_account_id FROM message_catchup").fetchall():
                r = self._load(c, account)
                self._tick(c, r, now)
                self._save(c, r)

    def report(self, account, value, *, now):
        with self.database.transaction() as c:
            r = self._load(c, account)
            previous = c.execute("SELECT report_seq FROM capture_report_receipts WHERE creator_account_id=? AND worker_instance_id=?",
                                 (account, value["worker_instance_id"])).fetchone()
            if previous is not None and previous[0] >= value["report_seq"]:
                return {"acknowledged_seq": value["report_seq"]}
            self._tick(c, r, now)
            if r["last_worker_instance_id"] is not None and r["last_worker_instance_id"] != value["worker_instance_id"]:
                self._trigger(c, r, r["last_report_at"] or now.isoformat())
            if sum(value["drops_since_last"].values()) > 0:
                self._trigger(c, r, r["last_report_at"] or now.isoformat())
            r.update(last_worker_instance_id=value["worker_instance_id"], last_report_at=now.isoformat(),
                     reported_observing=int(value["observing"]), report_reason=value["reason"])
            self._tick(c, r, now)
            if r["observing"]:
                r["last_observing_report_at"] = now.isoformat()
            counts = {**value["requests_since_last"], **value["drops_since_last"]}
            day = str(value["utc_day"])
            if day > now.date().isoformat():
                raise ValueError("capture report day is in the future")
            c.execute("INSERT OR IGNORE INTO capture_daily_counters(creator_account_id,utc_day) VALUES (?,?)", (account, day))
            automatic = sum(counts[name] for name in ("canary_list", "catchup_list", "catchup_messages"))
            c.execute("UPDATE capture_daily_counters SET " + ",".join(f"{name}={name}+?" for name in COUNTERS)
                      + ",automatic_pages=MAX(automatic_pages+?,?) WHERE creator_account_id=? AND utc_day=?",
                      (*[counts[name] for name in COUNTERS], automatic, value["automatic_pages_today"], account, day))
            c.execute("INSERT INTO capture_report_receipts VALUES (?,?,?) ON CONFLICT(creator_account_id,worker_instance_id) DO UPDATE SET report_seq=excluded.report_seq",
                      (account, value["worker_instance_id"], value["report_seq"]))
            self._save(c, r)
        return {"acknowledged_seq": value["report_seq"]}

    def _remaining(self, c, account, now):
        row = c.execute("SELECT automatic_pages FROM capture_daily_counters WHERE creator_account_id=? AND utc_day=?",
                        (account, now.date().isoformat())).fetchone()
        return max(0, self.policy.catchup_daily_page_cap - (0 if row is None else row[0]))

    @staticmethod
    def _configuration(c, account):
        row = c.execute("SELECT * FROM history_settings WHERE creator_account_id=?", (account,)).fetchone()
        return {} if row is None else dict(row)

    def _paused(self, c, r, now):
        if not r["supported"] or not self.policy.catchup_enabled:
            return "extension_outdated"
        config = self._configuration(c, r["creator_account_id"])
        desired = config.get("desired_state")
        if desired == "paused":
            return "user_paused"
        if desired in {None, "not_started", "revoked"}:
            return "consent_needed"
        if not self._live(r, now):
            return "extension_offline"
        if r["report_reason"] in PAUSED:
            return PAUSED[r["report_reason"]]
        if (config.get("effective_state") != "running"
            or config.get("required_config_revision") != config.get("effective_config_revision")
            or config.get("settings_revision") != config.get("effective_settings_revision")):
            return "applying_settings"
        return None

    def begin(self, account, value, *, now, required_config_revision=None):
        with self.database.transaction() as c:
            r = self._load(c, account)
            self._tick(c, r, now)
            def done(result):
                self._save(c, r)
                return result
            def deferred(reason, seconds=60):
                return done(dict(result="deferred", retry_after_seconds=max(1, seconds), reason=reason))
            if not self.policy.catchup_enabled or not r["supported"]:
                return done({"result": "not_needed"})
            config = self._configuration(c, account)
            required_revision = required_config_revision or config.get("effective_config_revision")
            if (self._paused(c, r, now) is not None or r["last_report_at"] is None
                or value["worker_instance_id"] != r["last_worker_instance_id"]
                or value["config_revision"] != required_revision):
                return deferred("not_runnable")
            active = self._active(c, r)
            renewal = active is not None and (value["active_check_id"] == active["check_id"] or value["request_id"] == active["request_id"])
            if active is not None and not renewal:
                return deferred("check_active")
            remaining = self._remaining(c, account, now)
            if remaining == 0:
                if renewal:
                    c.execute("UPDATE message_checks SET state='suspended',suspended_day=? WHERE check_id=?", (now.date().isoformat(), active["check_id"]))
                tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
                return deferred("daily_cap", int((tomorrow - now).total_seconds()))
            if renewal:
                active["lease_expires_at"] = (now + timedelta(seconds=self.policy.catchup_lease_seconds)).isoformat()
                c.execute("UPDATE message_checks SET state='active',suspended_day=NULL,lease_expires_at=?,agent_installation_id=?,agent_stream_id=? WHERE check_id=?",
                          (active["lease_expires_at"], r["agent_installation_id"], r["agent_stream_id"], active["check_id"]))
            else:
                if r["gap_open"]:
                    if self._baseline(c, account) is None:
                        return deferred("history_incomplete")
                    if not r["observing"] and r["blind_catchup_done"]:
                        return done({"result": "not_needed"})
                    if r["last_grant_at"] is not None:
                        wait = self.policy.catchup_grant_min_interval_minutes * 60 - (now - instant(r["last_grant_at"])).total_seconds()
                        if wait > 0:
                            return deferred("grant_interval", int(wait + 0.999))
                    kind = "catch_up"
                else:
                    if not r["observing"] or (r["next_canary_at"] is not None and now < instant(r["next_canary_at"])):
                        return done({"result": "not_needed"})
                    kind = "canary"
                active = dict(check_id=str(uuid4()), creator_account_id=account, kind=kind, epoch=r["gap_epoch"],
                              uncertain_since=r["uncertain_since"], granted_at=now.isoformat(), blind=int(not r["observing"]),
                              lease_expires_at=(now + timedelta(seconds=self.policy.catchup_lease_seconds)).isoformat(),
                              state="active", request_id=value["request_id"], agent_installation_id=r["agent_installation_id"], agent_stream_id=r["agent_stream_id"])
                c.execute("INSERT INTO message_checks(" + ",".join(active) + ") VALUES (" + ",".join("?" for _ in active) + ")", tuple(active.values()))
                r.update(active_check_id=active["check_id"], last_grant_at=now.isoformat(), incomplete=0)
            return done(dict(result="granted", check_id=active["check_id"], kind=active["kind"], gap_epoch=active["epoch"],
                             uncertain_since=active["uncertain_since"], granted_at=active["granted_at"], blind=bool(active["blind"]),
                             page_budget=min(remaining, self.policy.catchup_grant_page_chunk), lease_expires_at=active["lease_expires_at"], resume=renewal))

    def state(self, account, *, now=None):
        now = now or datetime.now(timezone.utc)
        with self.database.transaction() as c:
            r = self._load(c, account)
            self._tick(c, r, now)
            paused = self._paused(c, r, now)
            active = self._active(c, r)
            status, reason = "current", None
            if paused is not None:
                status, reason = "paused", paused
            elif active is not None and active["state"] == "active":
                status, reason = "checking", active["kind"]
            elif r["last_closed_at"] is None:
                status, reason = "never_checked", None
            elif r["gap_open"] or not r["observing"] or active is not None:
                status = "behind"
                reason = ("daily_cap" if self._remaining(c, account, now) == 0 else
                          "not_observing" if not r["observing"] else
                          "check_incomplete" if r["incomplete"] else "awaiting_check")
            self._save(c, r)
            return dict(status=status, reason=reason, gap_epoch=r["gap_epoch"], uncertain_since=r["uncertain_since"],
                        check_id=r["active_check_id"], last_closed_at=r["last_closed_at"],
                        observing_since=r["observing_since"], evaluated_at=now.isoformat())

    def supported(self, account):
        with self.database.read() as c:
            r = c.execute("SELECT supported FROM message_catchup WHERE creator_account_id=?", (account,)).fetchone()
            return bool(r is not None and r[0])

    def counters(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        names = ("automatic_pages", *COUNTERS)
        with self.database.read() as c:
            row = c.execute("SELECT " + ",".join(f"COALESCE(SUM({name}),0)" for name in names)
                            + " FROM capture_daily_counters WHERE utc_day=?", (now.date().isoformat(),)).fetchone()
            return dict(zip(names, row))

    @staticmethod
    def attribute_insert(c, account, check_id, sent_at):
        if check_id is None:
            return
        check = c.execute("""SELECT k.granted_at FROM message_checks k
            JOIN message_catchup g ON g.creator_account_id=k.creator_account_id AND g.active_check_id=k.check_id
            WHERE k.creator_account_id=? AND k.check_id=? AND k.state IN ('active','suspended')""", (account, str(check_id))).fetchone()
        if check is not None and instant(sent_at) <= instant(check[0]) - timedelta(seconds=120):
            c.execute("UPDATE message_checks SET inserted_old=inserted_old+1 WHERE check_id=?", (str(check_id),))

    def apply_evidence(self, c, account, evidence, now, *, key=None, checkpoint=0, snapshot=False):
        if snapshot:
            return False
        r = self._load(c, account)
        self._tick(c, r, instant(now))
        check = self._active(c, r)
        if (check is None or check["check_id"] != str(evidence["generation_id"])
            or key is None or (check["agent_installation_id"], check["agent_stream_id"]) != key.sql()[1:]):
            raise CatchupEvidenceError("check evidence does not belong to the active source")
        kind = evidence["type"]
        if evidence.get("final_source_seq", 0) > checkpoint:
            raise CatchupEvidenceError("check evidence precedes its committed source fence")
        if kind == "check.chat_reconciled":
            prior = c.execute("SELECT evidence_json FROM message_check_chats WHERE check_id=? AND chat_id=?", (check["check_id"], evidence["chat_id"])).fetchone()
            if prior is not None and prior[0] != packed(evidence):
                raise CatchupEvidenceError("reconciled chat evidence conflicts")
            c.execute("INSERT OR IGNORE INTO message_check_chats VALUES (?,?,?)", (check["check_id"], evidence["chat_id"], packed(evidence)))
        elif kind == "check.inventory_closed":
            if check["inventory_json"] is not None and check["inventory_json"] != packed(evidence):
                raise CatchupEvidenceError("check inventory evidence conflicts")
            c.execute("UPDATE message_checks SET inventory_json=? WHERE check_id=?", (packed(evidence), check["check_id"]))
        elif kind == "check.abandoned":
            c.execute("UPDATE message_checks SET state='abandoned',reason=? WHERE check_id=?", (evidence["reason"], check["check_id"]))
            r.update(active_check_id=None, incomplete=1)
        elif kind == "check.completed":
            if evidence["kind"] != check["kind"]:
                raise CatchupEvidenceError("check completion kind conflicts")
            inventory = json.loads(check["inventory_json"]) if check["inventory_json"] is not None else None
            reconciled = c.execute("SELECT COUNT(*) FROM message_check_chats WHERE check_id=?", (check["check_id"],)).fetchone()[0]
            if (check["kind"] == "catch_up" and inventory is None) or reconciled != (0 if inventory is None else inventory["changed"] + inventory["movers"]):
                raise CatchupEvidenceError("check completion lacks reconciled inventory")
            if check["kind"] == "canary":
                mismatch = check["inserted_old"] > 0
                cutoff = instant(check["granted_at"]) - timedelta(seconds=120)
                for head in evidence.get("heads", []):
                    if head.get("head_sent_at") is not None and instant(head["head_sent_at"]) > cutoff:
                        continue
                    if head.get("head_message_id") is not None:
                        found = c.execute("SELECT 1 FROM account_messages WHERE creator_account_id=? AND chat_id=? AND message_id=? AND is_deleted=0",
                                          (account, head["chat_id"], head["head_message_id"])).fetchone()
                    elif head.get("head_sent_at") is not None:
                        found = c.execute("SELECT 1 FROM account_messages WHERE creator_account_id=? AND chat_id=? AND julianday(sent_at)>=julianday(?) AND is_deleted=0 LIMIT 1",
                                          (account, head["chat_id"], head["head_sent_at"])).fetchone()
                    else:
                        continue
                    mismatch |= found is None
                if mismatch:
                    self._trigger(c, r, r["last_closed_at"] or check["granted_at"])
            elif check["blind"]:
                r["blind_catchup_done"] = 1
            elif r["gap_epoch"] == check["epoch"]:
                r.update(gap_open=0, uncertain_since=None, pending_uncertain_since=None, last_closed_at=now)
            else:
                r.update(uncertain_since=r["pending_uncertain_since"] or r["uncertain_since"], pending_uncertain_since=None)
            r.update(active_check_id=None, incomplete=0,
                     next_canary_at=(instant(now) + timedelta(minutes=self.policy.catchup_canary_interval_minutes)).isoformat())
            c.execute("UPDATE message_checks SET state='completed',completed_at=?,final_source_seq=?,completion_json=? WHERE check_id=?",
                      (now, evidence["final_source_seq"], packed(evidence), check["check_id"]))
        self._save(c, r)
        return False
