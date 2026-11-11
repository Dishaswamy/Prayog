"""Node registration, identity, heartbeats and admin state changes (GPU requirements section 6)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

import models as orm
from logic.common.errors import BadRequestError, ConflictError, NotFoundError
from logic.gpu.scheduler import nodes
from logic.gpu.scheduler.domain import AlertKind, JobState, NodeState
from test_cases.gpu.scheduler.factories import NOW

pytestmark = pytest.mark.db


def register(db, labs, name="nlp-01", units=4, shared=True, teaching=False, owner="NLP"):
    return nodes.register_node(db, name, labs[owner], "shared" if shared else "reserved", teaching, units, now=NOW)


def enrolled(db, labs, **kw):
    reg = register(db, labs, **kw)
    node, credential = nodes.enrol(db, reg.enrolment_token, now=NOW)
    return node.id, credential


def state(db, node_id):
    db.expire_all()
    return db.get(orm.Node, node_id).state


def open_alert_kinds(db, node_id):
    db.expire_all()
    return sorted(a.kind for a in nodes.open_alerts(db) if a.node_id == node_id)


# ── registration (FR-GPU-025, 110, 181) ────────────────────────────────

def test_registration_creates_the_node_and_its_units_pending_an_agent(db, labs):  # FR-GPU-025, 026
    reg = register(db, labs, units=3)

    node = db.get(orm.Node, reg.node.id)
    assert (node.state, node.unit_count, node.owner_lab_id) == (NodeState.PENDING_AGENT, 3, labs["NLP"])
    assert db.scalars(select(orm.GpuUnit.unit_index).where(orm.GpuUnit.node_id == node.id)).all() == [0, 1, 2]
    assert reg.enrolment_token and node.enrolment_token_hash != reg.enrolment_token     # only the hash is stored
    assert node.enrolment_expires_at == NOW + timedelta(hours=24)


@pytest.mark.parametrize("kwargs, message", [
    ({"shared": False, "teaching": True}, "teaching-pool node must be shared"),   # FR-GPU-181
    ({"units": 0}, "at least one GPU"),
])
def test_invalid_registrations_are_refused(db, labs, kwargs, message):
    with pytest.raises(BadRequestError, match=message):
        register(db, labs, **kwargs)


def test_unknown_owner_lab_and_duplicate_names_are_refused(db, labs):  # FR-GPU-025
    with pytest.raises(BadRequestError, match="Unknown owner lab"):
        nodes.register_node(db, "x", 9999, "shared", False, 2, now=NOW)
    register(db, labs)
    with pytest.raises(ConflictError, match="already registered"):
        register(db, labs)


# ── identity (architecture section 6.6) ────────────────────────────────

def test_an_enrolment_token_works_once(db, labs):
    reg = register(db, labs)
    node, credential = nodes.enrol(db, reg.enrolment_token, now=NOW)
    assert nodes.authenticate(db, credential).id == node.id
    with pytest.raises(BadRequestError, match="invalid, used or expired"):
        nodes.enrol(db, reg.enrolment_token, now=NOW)


def test_an_expired_enrolment_token_is_refused(db, labs):
    reg = register(db, labs)
    with pytest.raises(BadRequestError):
        nodes.enrol(db, reg.enrolment_token, now=NOW + timedelta(hours=25))


@pytest.mark.parametrize("credential", [None, "", "not-a-credential"])
def test_unknown_credentials_authenticate_nobody(db, labs, credential):
    enrolled(db, labs)
    assert nodes.authenticate(db, credential) is None


# ── heartbeats (FR-GPU-077, 110, 136) ──────────────────────────────────

def test_the_first_heartbeat_with_the_declared_units_makes_the_node_available(db, labs):  # FR-GPU-110
    node_id, _ = enrolled(db, labs, units=4)

    result = nodes.heartbeat(db, node_id, 4, [], now=NOW)

    assert (result.state, result.became_available) == (NodeState.AVAILABLE, True)
    assert db.get(orm.Node, node_id).last_heartbeat_at == NOW


def test_a_unit_count_mismatch_keeps_the_node_pending_and_alerts(db, labs):  # FR-GPU-110, 174
    node_id, _ = enrolled(db, labs, units=4)

    nodes.heartbeat(db, node_id, 3, [], now=NOW)
    nodes.heartbeat(db, node_id, 3, [], now=NOW)                         # still one alert, not two

    assert state(db, node_id) == NodeState.PENDING_AGENT
    assert open_alert_kinds(db, node_id) == [AlertKind.UNIT_COUNT_MISMATCH]
    nodes.heartbeat(db, node_id, 4, [], now=NOW)
    assert state(db, node_id) == NodeState.AVAILABLE and open_alert_kinds(db, node_id) == []


def test_a_rejoining_node_must_terminate_orphans_before_it_is_available(db, labs):  # FR-GPU-136
    node_id, _ = enrolled(db, labs)
    nodes.heartbeat(db, node_id, 4, [], now=NOW)
    node = db.get(orm.Node, node_id)
    nodes.transition(node, NodeState.DOWN, NOW)
    db.commit()

    first = nodes.heartbeat(db, node_id, 4, [77], now=NOW)               # still runs a container from before
    assert (first.state, first.terminate_allocation_ids) == (NodeState.DOWN, (77,))
    second = nodes.heartbeat(db, node_id, 4, [], now=NOW)
    assert (second.state, second.became_available) == (NodeState.AVAILABLE, True)


# ── drain, return, decommission (FR-GPU-112 to 115, 168 to 172) ────────

def test_drain_and_return_to_service(db, labs):  # FR-GPU-168, 169
    node_id, _ = enrolled(db, labs)
    nodes.heartbeat(db, node_id, 4, [], now=NOW)

    nodes.drain(db, node_id, "soft", "replace fan", now=NOW)
    assert state(db, node_id) == NodeState.DRAINING
    nodes.heartbeat(db, node_id, 4, [], now=NOW)
    assert state(db, node_id) == NodeState.DRAINING                     # a heartbeat alone does not return it

    nodes.return_to_service(db, node_id)
    nodes.heartbeat(db, node_id, 4, [], now=NOW)
    node = db.get(orm.Node, node_id)
    assert (node.state, node.return_requested, node.drain_mode) == (NodeState.AVAILABLE, False, None)


def test_a_returning_node_with_fewer_units_stays_drained_until_confirmed(db, labs):  # FR-GPU-170
    node_id, _ = enrolled(db, labs, units=4)
    nodes.heartbeat(db, node_id, 4, [], now=NOW)
    nodes.drain(db, node_id, "soft", "board swap", now=NOW)
    nodes.return_to_service(db, node_id)

    nodes.heartbeat(db, node_id, 3, [], now=NOW)
    assert state(db, node_id) == NodeState.DRAINING
    assert open_alert_kinds(db, node_id) == [AlertKind.UNIT_COUNT_CHANGED]

    nodes.confirm_unit_count(db, node_id, 3, now=NOW)
    nodes.heartbeat(db, node_id, 3, [], now=NOW)
    assert state(db, node_id) == NodeState.AVAILABLE
    units = db.scalars(select(orm.GpuUnit).where(orm.GpuUnit.node_id == node_id).order_by(orm.GpuUnit.unit_index))
    assert [(u.unit_index, u.retired_at is not None) for u in units] == [(0, False), (1, False), (2, False), (3, True)]


def test_confirming_more_units_adds_them(db, labs):  # FR-GPU-170
    node_id, _ = enrolled(db, labs, units=2)
    nodes.confirm_unit_count(db, node_id, 4, now=NOW)
    assert db.scalars(select(orm.GpuUnit.unit_index).where(orm.GpuUnit.node_id == node_id)).all() == [0, 1, 2, 3]


@pytest.mark.parametrize("before", [NodeState.AVAILABLE, NodeState.DRAINING, NodeState.DOWN])
def test_decommission_from_any_permitted_state(db, labs, before):  # FR-GPU-112, 172
    node_id, _ = enrolled(db, labs)
    node = db.get(orm.Node, node_id)
    node.state = before
    db.commit()

    nodes.decommission(db, node_id, "soft", "end of warranty", now=NOW)

    node = db.get(orm.Node, node_id)
    assert (node.state, node.state_reason) == (NodeState.DECOMMISSIONING, "end of warranty")


def test_escalate_and_abort_decommission(db, labs):  # FR-GPU-171
    node_id, _ = enrolled(db, labs)
    nodes.heartbeat(db, node_id, 4, [], now=NOW)
    nodes.drain(db, node_id, "hard", "dead GPU", now=NOW)

    nodes.escalate(db, node_id, "not worth repairing", now=NOW)
    assert state(db, node_id) == NodeState.DECOMMISSIONING
    nodes.abort_decommission(db, node_id, "spare part arrived", now=NOW)
    assert state(db, node_id) == NodeState.DRAINING


@pytest.mark.parametrize("action", [
    lambda db, n: nodes.drain(db, n, "soft", "x", now=NOW),                   # PENDING_AGENT cannot drain
    lambda db, n: nodes.return_to_service(db, n),                             # not DRAINING
    lambda db, n: nodes.escalate(db, n, "x", now=NOW),                        # not DRAINING
    lambda db, n: nodes.abort_decommission(db, n, "x", now=NOW),              # not DECOMMISSIONING
    lambda db, n: nodes.decommission(db, n, "soft", "x", now=NOW),            # PENDING_AGENT cannot decommission
])
def test_invalid_transitions_are_refused_and_change_nothing(db, labs, action):  # A-73, FR-GPU-028
    node_id, _ = enrolled(db, labs)
    with pytest.raises(ConflictError):
        action(db, node_id)
    assert state(db, node_id) == NodeState.PENDING_AGENT


@pytest.mark.parametrize("action", [
    lambda db, n: nodes.drain(db, n, "soft", "  ", now=NOW),
    lambda db, n: nodes.decommission(db, n, "hard", "", now=NOW),
])
def test_admin_actions_need_a_reason(db, labs, action):  # FR-GPU-112, 168
    node_id, _ = enrolled(db, labs)
    with pytest.raises(BadRequestError, match="reason is required"):
        action(db, node_id)


def test_unknown_nodes_are_not_found(db, labs):
    with pytest.raises(NotFoundError):
        nodes.drain(db, 9999, "soft", "x", now=NOW)


# ── alerts (FR-GPU-174) ────────────────────────────────────────────────

def test_an_admin_can_acknowledge_an_alert_once(db, labs):
    node_id, _ = enrolled(db, labs, units=4)
    nodes.heartbeat(db, node_id, 2, [], now=NOW)
    [alert] = nodes.open_alerts(db)

    nodes.acknowledge_alert(db, alert.id, "known: 2 GPUs out for RMA", now=NOW)

    assert nodes.open_alerts(db) == []
    with pytest.raises(ConflictError):
        nodes.acknowledge_alert(db, alert.id, "again", now=NOW)
    with pytest.raises(NotFoundError):
        nodes.acknowledge_alert(db, 9999, "x", now=NOW)


# ── jobs no node can run (FR-GPU-117) ──────────────────────────────────

def test_jobs_larger_than_any_remaining_node_are_cancelled(db, seed, labs):  # FR-GPU-117
    seed.node(labs["NLP"], units=4)
    big = seed.job(labs["NLP"], gpus=8)
    fits = seed.job(labs["NLP"], gpus=4)
    seed.commit()

    assert nodes.cancel_unsatisfiable_jobs(db, NOW) == [big]
    db.expire_all()
    assert (db.get(orm.Job, big).state, db.get(orm.Job, big).state_reason) == (JobState.CANCELLED, "NO_CAPABLE_NODE")
    assert db.get(orm.Job, fits).state == JobState.QUEUED


def test_no_cancellation_while_a_node_may_still_come_back(db, seed, labs):  # FR-GPU-117
    # Only a 4-unit node is in repair; it may come back with more units, so the 6-GPU job waits.
    seed.node(labs["NLP"], units=2)
    seed.node(labs["NLP"], units=4, state=NodeState.DRAINING)
    j = seed.job(labs["NLP"], gpus=6)
    seed.commit()

    assert nodes.cancel_unsatisfiable_jobs(db, NOW) == []
    assert db.get(orm.Job, j).state == JobState.QUEUED


def test_node_summaries_count_units_and_alerts(db, seed, labs):
    node_id, _ = enrolled(db, labs, units=4)
    nodes.heartbeat(db, node_id, 3, [], now=NOW)

    [summary] = [s for s in nodes.node_summaries(db) if s.node.id == node_id]

    assert (summary.units, summary.free_units, summary.open_alerts) == (4, 4, 1)
