"""周期 phase_day：余温期无代表股时仍应累加。"""
from src.engine.cycle import CycleEngine, CyclePhase, TrackedStock


def test_afterglow_without_representative_increments_phase_day():
    eng = CycleEngine()
    eng.phase = CyclePhase.AFTERGLOW
    eng.phase_day = 1
    eng.phase_entered_date = "2026-05-10"
    eng.representative = None
    eng.tracked = {
        "301666": TrackedStock(
            code="301666",
            name="A",
            gain_10d=80.0,
            sustain_days=5,
            peak_gain=120.0,
            is_main_board=False,
        ),
    }

    eng._transition()

    assert eng.phase == CyclePhase.AFTERGLOW
    assert eng.phase_day == 2


def test_afterglow_rep_not_in_tracked_increments_phase_day():
    eng = CycleEngine()
    eng.phase = CyclePhase.AFTERGLOW
    eng.phase_day = 3
    eng.representative = TrackedStock(
        code="301666",
        name="A",
        gain_10d=35.0,
        peak_gain=200.0,
        is_main_board=False,
    )
    eng.tracked = {
        "001259": TrackedStock(
            code="001259",
            name="B",
            gain_10d=105.0,
            sustain_days=5,
            peak_gain=105.0,
            is_main_board=True,
        ),
    }

    eng._transition()

    assert eng.phase == CyclePhase.AFTERGLOW
    assert eng.phase_day == 4
