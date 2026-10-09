"""Who is held back by a shared link, and what the time-left forecast says, with made-up drives."""
from usblockbox import eta, shares
from usblockbox.config import Settings
from usblockbox.eta import Timings
from usblockbox.pipeline import plan_steps

MB, GB = 1e6, 1e9
ROOT = "PCIROOT(0)#PCI(1400)#USBROOT(0)"
HUB = ROOT + "#USB(1)"


def _plan(passes=3):
    s = Settings(); s.overwrite_passes = passes
    return plan_steps(s)


def _chain(hub_speed, own_speed=3, hub=HUB, port=1):
    return [{"kind": "drive", "path": hub + "#USB(%d)" % port, "speed": own_speed},
            {"kind": "hub", "path": hub, "speed": hub_speed}]


def _entry(i, hub_speed=2, label=None, frac=0.0, passes=3, recent=None, waiting=False, gb=32):
    plan = _plan(passes)
    return shares.Entry(i, plan, label or plan[0], frac, gb * GB, passes, False, _chain(hub_speed, port=i + 1),
                        waiting, recent)


def _timings(over_s_per_gb=40.0, enc_s_per_gb=30.0):
    t = Timings()
    for _ in range(3):
        t.record("overwrite", over_s_per_gb); t.record("encrypt", enc_s_per_gb)
    return t


def test_four_waiting_drives_on_a_usb2_hub_are_told_they_share_it():
    out = shares.compute([_entry(i, waiting=True) for i in range(4)], _timings(2.0, 2.0))   # drives that manage 500 MB/s alone
    s = out[0]
    assert s.sharers == 3 and "USB 2.0" in s.link and s.link_cap == 35 * MB
    assert s.factor > 10                                             # each is held back far below its own speed
    assert abs(s.rate - 8.75 * MB) < 1e4


def test_the_same_drives_on_a_usb3_hub_do_not_share_a_problem():
    out = shares.compute([_entry(i, hub_speed=3, waiting=True) for i in range(4)], _timings(40.0, 30.0))
    assert all(s.sharers == 0 and s.factor == 1.0 for s in out.values())      # 4 x 25 MB/s fits in 400 MB/s


def test_slow_cheap_drives_do_not_saturate_even_a_usb2_hub():
    out = shares.compute([_entry(i, waiting=True) for i in range(2)], _timings(200.0, 200.0))   # 5 MB/s each
    assert all(s.sharers == 0 for s in out.values())                          # 10 MB/s < 35 MB/s: the hub is not the limit


def test_a_drive_that_is_slow_while_the_link_has_room_is_the_drive_not_the_link():
    plan = _plan(0)
    enc = "Encrypt"
    es = [shares.Entry(0, plan, enc, 0.5, 32 * GB, 0, False, _chain(3), False, recent_rate=0.5 * MB / (32 * GB)),   # 0.5 MB/s
          shares.Entry(1, plan, enc, 0.5, 32 * GB, 0, False, _chain(3, port=2), False, recent_rate=30 * MB / (32 * GB))]
    out = shares.compute(es, _timings(40.0, 30.0))
    assert out[0].drive_limited and not out[1].drive_limited
    assert out[0].factor == 1.0                                               # nobody is sharing a full link


def test_forecast_for_a_drive_speeds_up_when_neighbours_finish():
    plan = _plan(0)
    enc = "Encrypt"
    near_done = [shares.Entry(i, plan, enc, 0.95, 32 * GB, 0, False, _chain(2, port=i + 1), False) for i in range(3)]
    big = shares.Entry(3, plan, enc, 0.0, 32 * GB, 0, False, _chain(2, port=4), False)
    t = _timings(2.0, 2.0)
    with_others = shares.compute(near_done + [big], t)[3]
    alone = shares.compute([big], t)[3]
    assert alone.finish < with_others.finish
    flat = 32 * GB / (with_others.rate)                                       # "stay at today's share the whole way"
    assert with_others.finish < flat                                          # the others finish soon, so it speeds up


def test_nothing_to_do_gives_nothing():
    assert shares.compute([], Timings()) == {}
    plan = _plan(3)
    done = shares.Entry(0, plan, "Verify", 0.0, 32 * GB, 3, False, _chain(2), False)
    assert shares.compute([done], Timings()) == {}                            # no long work left


def test_missing_or_unknown_link_data_is_fine():
    plan = _plan(1)
    es = [shares.Entry(0, plan, plan[0], 0.0, 32 * GB, 1, False, [], True),
          shares.Entry(1, plan, plan[0], 0.0, 32 * GB, 1, False, [{"kind": "hub", "path": HUB, "speed": None}], True)]
    out = shares.compute(es, Timings())
    assert out[0].sharers == 0 and out[1].sharers == 0 and out[0].finish and out[1].finish
