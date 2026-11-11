"""Until when a queued job waits for its own lab's GPUs (FR-GPU-108, A-08, A-14)."""

from datetime import timedelta

from logic.gpu.scheduler.domain import NodeState
from logic.gpu.scheduler.waiting import p0_guaranteed_free_at
from test_cases.gpu.scheduler.factories import NLP, NOW, ROBOTICS, job, node, window

H = timedelta(hours=1)


def busy(n, *hours):
    """busy_until for the node's occupied units, in unit order: unit i free at NOW + hours[i]."""
    occupied = [u for u in n.units if not u.free]
    return {u.unit_id: NOW + h * H for u, h in zip(occupied, hours, strict=True)}


def test_waits_for_the_kth_earliest_release_on_its_own_node():  # FR-GPU-108
    own = node(owner=NLP, units=4, free=1)                     # 3 busy, 1 free
    j = job(lab=NLP, gpus=3)                                   # needs 2 more

    assert p0_guaranteed_free_at(j, [own], busy(own, 5, 2, 8), NOW) == NOW + 5 * H


def test_takes_the_earliest_of_several_own_nodes():
    a = node(node_id=1, owner=NLP, units=2, free=0)
    b = node(node_id=2, owner=NLP, units=2, free=0)
    j = job(lab=NLP, gpus=2)

    assert p0_guaranteed_free_at(j, [a, b], busy(a, 6, 7) | busy(b, 3, 4), NOW) == NOW + 4 * H


def test_nothing_to_wait_for_when_an_own_node_fits_now():
    own = node(owner=NLP, units=4, free=2)
    assert p0_guaranteed_free_at(job(lab=NLP, gpus=2), [own], busy(own, 1, 1), NOW) is None


def test_other_labs_nodes_never_count():  # P0 is own-lab only
    theirs = node(owner=ROBOTICS, units=4, free=0)
    assert p0_guaranteed_free_at(job(lab=NLP, gpus=2), [theirs], busy(theirs, 1, 1, 1, 1), NOW) is None


def test_no_time_when_something_other_than_occupancy_blocks_it():
    too_small = node(node_id=1, owner=NLP, units=2, free=0)
    down = node(node_id=2, owner=NLP, units=4, free=0, state=NodeState.DOWN)
    j = job(lab=NLP, gpus=3)
    times = busy(too_small, 1, 1) | busy(down, 1, 1, 1, 1)
    assert p0_guaranteed_free_at(j, [too_small, down], times, NOW) is None


def test_units_under_maintenance_are_never_counted_on():
    own = node(owner=NLP, units=2, free=0, maintenance=(window(start_in_h=48),))
    # Maintenance far in the future still makes those units uncertain: no promise is made.
    assert p0_guaranteed_free_at(job(lab=NLP, gpus=2, walltime_h=1), [own], busy(own, 1, 1), NOW) is None


def test_no_time_when_a_busy_unit_has_no_bound():
    own = node(owner=NLP, units=2, free=0)
    occupied = [u.unit_id for u in own.units]
    assert p0_guaranteed_free_at(job(lab=NLP, gpus=2), [own], {occupied[0]: NOW + H}, NOW) is None
