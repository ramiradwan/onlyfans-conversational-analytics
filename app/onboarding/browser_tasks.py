"""Task-bound browser observations; only domain services can authorize Full work."""
from __future__ import annotations

import json
import re

from app.onboarding.admission import AdmissionRefused, ScopedAdmissions
from app.onboarding.contract import canonical
from app.persistence.onboarding import require_journey
from app.provisioning.events import events

OBSERVATION_FIELDS = frozenset({"profile", "task_id", "journey_id", "admission_generation", "extension_id",
    "instance_id", "epoch", "revision", "account_generation", "consent_generation", "draft_scope_id",
    "disclosure_bundle_id", "state", "creator_account_id"})
BROWSER_STATES = frozenset({"unavailable", "needs-consent", "needs-permission", "needs-account", "ready", "paused"})
LABEL_FIELDS = {"creator_display_name": 80, "creator_username": 128}


def validate_observation(value):
    try:
        if (not isinstance(value, dict) or not OBSERVATION_FIELDS <= set(value)
                or not set(value) <= OBSERVATION_FIELDS | LABEL_FIELDS.keys() or value["profile"] != "browser-task-observation.v1"):
            raise ValueError()
        for name in ("task_id", "journey_id", "admission_generation", "instance_id", "epoch", "draft_scope_id"):
            require_journey(value[name])
        for name in ("revision", "account_generation", "consent_generation"):
            if type(value[name]) is not int or not 0 <= value[name] <= 9007199254740991:
                raise ValueError()
        if (re.fullmatch(r"[a-p]{32}", value["extension_id"]) is None
                or re.fullmatch(r"[a-f0-9]{64}", value["disclosure_bundle_id"]) is None
                or value["state"] not in BROWSER_STATES):
            raise ValueError()
        creator = value["creator_account_id"]
        if creator is not None and (not isinstance(creator, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", creator) is None):
            raise ValueError()
        if (value["state"] in {"ready", "paused"}) != (creator is not None):
            raise ValueError()
        for name, limit in LABEL_FIELDS.items():
            if name in value and (creator is None or not isinstance(value[name], str)
                    or not 1 <= len(value[name]) <= limit or value[name] != value[name].strip().strip("\ufeff")
                    or any(ord(character) < 32 or 127 <= ord(character) <= 159
                        or 0x202a <= ord(character) <= 0x202e or 0x2066 <= ord(character) <= 0x2069
                        for character in value[name])):
                raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AdmissionRefused("browser_observation_invalid") from None
    return value


def creator_label(observation, creator_id):
    if observation is None or creator_id is None or observation.get("creator_account_id") != creator_id:
        return None
    value = validate_observation(observation)
    name = value.get("creator_display_name")
    username = value.get("creator_username")
    if not name and not username:
        return None
    return {"creator_id": creator_id, "display_name": name, "username": username}


def saved_creator_label(store, creator_id):
    """Return display metadata only from this installation's completed account."""
    from app.persistence.continuation_selection import ContinuationSelectionStore, ContinuationSelectionUnavailable
    with store.database.read() as connection:
        try:
            target = ContinuationSelectionStore(store).select_in_transaction(connection)
        except ContinuationSelectionUnavailable:
            return None
        if target.candidate.creator_account_id != creator_id or target.binding_revoked_at is not None:
            return None
        rows = connection.execute("SELECT o.owner_scope_json,c.binding_json FROM desktop_setup_operations o "
            "JOIN desktop_browser_ceremonies c ON c.operation_id=o.operation_id "
            "WHERE o.action='account.connect' AND o.state='completed' LIMIT 129").fetchall()
    if len(rows) > 128:
        return None
    labels = []
    for row in rows:
        try:
            scope, binding = json.loads(row["owner_scope_json"]), json.loads(row["binding_json"])
            if (scope.get("creator_account_id") != creator_id
                    or binding.get("intended_creator_id") != creator_id
                    or binding.get("installation_id") != target.claim.installation_id
                    or binding.get("installation_key_id") != target.key.installation_key_id
                    or binding.get("installation_key_jkt") != target.key.installation_key_jkt
                    or binding.get("organization_id", target.claim.organization_id) != target.claim.organization_id
                    or scope.get("association_request_id") not in {None, target.candidate.association_request_id}):
                continue
            label = creator_label(scope.get("browser"), creator_id)
        except (ValueError, TypeError, AttributeError):
            continue
        if label is not None and label not in labels:
            labels.append(label)
    # Ambiguous labels cannot be selected by insertion order or age.
    return labels[0] if len(labels) == 1 else None


class BrowserTasks:
    def __init__(self, admissions: ScopedAdmissions, *, extension_id: str | None):
        if extension_id is not None and re.fullmatch(r"[a-p]{32}", extension_id) is None:
            raise ValueError("Invalid supported extension")
        self.admissions, self.extension_id = admissions, extension_id
        self.store = admissions.authentication

    def prepare(self, operation, purpose, *, deadline=None):
        if self.extension_id is None:
            raise AdmissionRefused("browser_unavailable")
        task_id = self.admissions.prepare_accepted_task(operation["operation_id"], purpose=purpose,
            deadline=operation["expires_at"] if deadline is None else deadline)
        events.publish()
        return task_id

    def current_native(self, session):
        with self.store.database.read() as connection:
            row, parent = self.admissions._require(connection, session.identifier, role="desktop", generation=session.generation)
            tasks = connection.execute("SELECT t.* FROM desktop_browser_tasks t JOIN desktop_setup_operations o "
                "ON o.operation_id=t.operation_id WHERE t.journey_id=? AND t.expires_at>? "
                "AND t.state NOT IN ('cancelled','expired') AND o.state IN ('accepted','waiting','outcome_unknown') "
                "ORDER BY o.created_at,t.task_id", (parent["journey_id"], self.admissions._now())).fetchall()
            return [self.public(task) for task in tasks]

    @staticmethod
    def public(task):
        return {name: task[name] for name in ("task_id", "journey_id", "operation_id", "purpose", "scope_digest",
            "source_revision", "admission_generation", "state", "expires_at")}

    def attachment(self, task):
        with self.store.database.read() as connection:
            root = connection.execute("SELECT entry_source FROM desktop_setup_journeys WHERE journey_id=?",
                (task["journey_id"],)).fetchone()
            operation = connection.execute("SELECT owner_scope_json FROM desktop_setup_operations WHERE operation_id=?",
                (task["operation_id"],)).fetchone()
        if root is None or operation is None:
            raise AdmissionRefused("browser_scope_changed")
        scope = json.loads(operation["owner_scope_json"])
        browser = scope.get("browser")
        participant = None if browser is None else {key: browser[key] for key in (
            "instance_id", "draft_scope_id", "disclosure_bundle_id")}
        return {"entry_source": root["entry_source"], "participant": participant,
            "creator_account_id": scope.get("creator_account_id")}

    def observe(self, session, observation):
        """Receive permitted extension observations from this task's browser helper.

        This records presentation evidence. It neither records collection consent
        nor grants installation, account, capture, pairing or operator authority.
        """
        value = validate_observation(observation)
        with self.store.database.transaction() as connection:
            _, task = self.admissions._require(connection, session.identifier, role="browser_task", generation=session.generation)
            if (task["purpose"] not in {"browser.access", "browser.identity"}
                    or any(value[name] != task[name] for name in ("task_id", "journey_id", "admission_generation"))
                    or value["extension_id"] != self.extension_id):
                raise AdmissionRefused("browser_scope_changed")
            prior = connection.execute("SELECT * FROM desktop_browser_observations WHERE task_id=?", (task["task_id"],)).fetchone()
            encoded = canonical(value).decode()
            if prior is not None and prior["admission_generation"] == task["admission_generation"]:
                previous = json.loads(prior["observation_json"])
                if (value["epoch"] != prior["epoch"] or value["revision"] < prior["revision"]
                        or any(value[name] != previous[name] for name in ("instance_id", "draft_scope_id", "disclosure_bundle_id"))
                        or any(value[name] < previous[name] for name in ("account_generation", "consent_generation"))):
                    raise AdmissionRefused("browser_scope_changed")
                if value["revision"] == prior["revision"]:
                    if encoded != prior["observation_json"]:
                        raise AdmissionRefused("browser_scope_changed")
                    return
            connection.execute("INSERT INTO desktop_browser_observations VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "admission_generation=excluded.admission_generation,epoch=excluded.epoch,revision=excluded.revision,observation_json=excluded.observation_json",
                (task["task_id"], task["admission_generation"], value["epoch"], value["revision"], encoded))
            self.admissions._require(connection, session.identifier, role="browser_task", generation=session.generation)
        events.publish()

    def read(self, journey_id, *, operation_id=None):
        if journey_id is None:
            return None
        with self.store.database.read() as connection:
            rows = connection.execute("SELECT o.observation_json FROM desktop_browser_observations o "
                "JOIN desktop_browser_tasks t ON t.task_id=o.task_id "
                "JOIN desktop_presentations p ON p.generation=t.desktop_generation "
                "WHERE t.journey_id=? AND t.admission_generation=o.admission_generation AND t.expires_at>? "
                "AND (? IS NULL OR t.operation_id=?) "
                "AND t.state NOT IN ('cancelled','expired') AND p.state='active' AND p.expires_at>?",
                (journey_id, self.admissions._now(), operation_id, operation_id, self.admissions._now())).fetchall()
        if len(rows) != 1:
            return None
        return validate_observation(json.loads(rows[0]["observation_json"]))

    def complete(self, operation_id):
        """Called only after the coordinator reads the relevant domain outcome."""
        with self.store.database.transaction() as connection:
            connection.execute("UPDATE desktop_browser_tasks SET state='completed' WHERE operation_id=? AND state IN ('pending','accepted','outcome_unknown')",
                               (require_journey(operation_id),))
        events.publish()

    def next_expiry(self):
        with self.store.database.read() as connection:
            row = connection.execute("SELECT min(expires_at) FROM desktop_browser_tasks WHERE expires_at>?",
                                     (self.admissions._now(),)).fetchone()
        return None if row[0] is None else max(0, row[0] - self.admissions._now())
