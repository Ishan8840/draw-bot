import os
import pytest
from scripts.run_mujoco import main


def launch(monkeypatch, args):
    import uvicorn
    called=[]
    monkeypatch.setattr('sys.argv',['run_mujoco',*args])
    monkeypatch.setattr(uvicorn,'run',lambda *a,**kw:called.append((a,kw)))
    # Track and restore launcher mutations, without starting a socket server.
    monkeypatch.setenv('ASTRA_EXECUTOR','virtual')
    monkeypatch.setenv('ASTRA_SCENE_DB','test')
    main()
    assert called and os.environ['ASTRA_EXECUTOR']=='mujoco'
    return float(os.environ['SIM_CHALK_WEAR_RATE'])


def test_launcher_enables_gradual_wear(monkeypatch):
    monkeypatch.delenv('SIM_CHALK_WEAR_RATE',raising=False)
    assert launch(monkeypatch,[])==.002


def test_explicit_zero_disables_wear(monkeypatch):
    monkeypatch.setenv('SIM_CHALK_WEAR_RATE','0')
    assert launch(monkeypatch,[])==0


def test_cli_overrides_old_zero_setting(monkeypatch):
    monkeypatch.setenv('SIM_CHALK_WEAR_RATE','0')
    assert launch(monkeypatch,['--chalk-wear-rate','0.02'])==.02


@pytest.mark.parametrize('value',['nan','inf','-0.1','0.2','invalid'])
def test_bad_wear_setting_fails_before_startup(monkeypatch,value):
    monkeypatch.setenv('SIM_CHALK_WEAR_RATE',value)
    with pytest.raises(SystemExit): launch(monkeypatch,[])


def test_gradual_wear_really_shortens_the_contact_tip():
    pytest.importorskip('mujoco')
    from backend.simulation.robot import RobotSimulation
    from backend.models import Line
    from backend.geometry import flatten
    robot=RobotSimulation()
    try:
        robot.configure_chalk(wear_rate=.002)
        original=robot.model.geom_pos[robot.nib_id].copy()
        path=flatten(Line(type='line',start={'x':.25,'y':.5},end={'x':.75,'y':.5}))
        for _ in range(1000):
            result=robot.batch('wear-check',path,.25,.05)
            assert result['fault'] is None
            if result['done']: break
        assert result['done'] and robot.world_ink['wear-check']
        assert robot.chalk_loss_m>0.0001
        assert robot.model.geom_pos[robot.nib_id][0]>original[0]+0.0001
        assert 'chalk_loss_m' not in robot.telemetry()
    finally: robot.close()
