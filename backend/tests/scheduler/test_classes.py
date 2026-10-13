"""PRAYOG-50 — priority class resolver (FR-GPU-036 to 042)."""

from scheduler.classes import owners_with_queued_work, resolve_class
from scheduler.domain import PriorityClass
from tests.scheduler.factories import CENTRAL, COURSE_GROUP, NLP, ROBOTICS, VISION, job, node

NOBODY_WAITING: frozenset[int] = frozenset()


def test_own_lab_node_is_p0():
    assert resolve_class(job(lab=NLP), node(owner=NLP), NOBODY_WAITING) == PriorityClass.P0


def test_guest_on_shared_node_with_idle_owner_is_p1():
    assert resolve_class(job(lab=NLP), node(owner=ROBOTICS), NOBODY_WAITING) == PriorityClass.P1


def test_guest_on_shared_node_with_waiting_owner_is_p2():
    assert resolve_class(job(lab=NLP), node(owner=ROBOTICS), frozenset({ROBOTICS})) == PriorityClass.P2


def test_course_job_on_teaching_node_is_p3():
    teaching = node(owner=CENTRAL, teaching=True)
    assert resolve_class(job(lab=COURSE_GROUP, course=True), teaching, NOBODY_WAITING) == PriorityClass.P3


def test_class_belongs_to_the_pair_not_the_job():
    j = job(lab=NLP)
    waiting = frozenset({ROBOTICS})
    assert resolve_class(j, node(node_id=1, owner=NLP), waiting) == PriorityClass.P0
    assert resolve_class(j, node(node_id=2, owner=ROBOTICS), waiting) == PriorityClass.P2
    assert resolve_class(j, node(node_id=3, owner=VISION), waiting) == PriorityClass.P1


def test_no_class_on_someone_elses_reserved_node():
    assert resolve_class(job(lab=NLP), node(owner=ROBOTICS, shared=False), NOBODY_WAITING) is None


def test_no_class_for_research_on_teaching_node():
    assert resolve_class(job(lab=NLP), node(owner=CENTRAL, teaching=True), NOBODY_WAITING) is None


def test_owners_waiting_counts_any_queued_research_job():
    queued = [job(job_id=1, lab=ROBOTICS), job(job_id=2, lab=COURSE_GROUP, course=True)]
    assert owners_with_queued_work(queued) == frozenset({ROBOTICS})


def test_classes_order_p0_first():
    assert sorted([PriorityClass.P3, PriorityClass.P0, PriorityClass.P2, PriorityClass.P1]) == [
        PriorityClass.P0, PriorityClass.P1, PriorityClass.P2, PriorityClass.P3,
    ]
