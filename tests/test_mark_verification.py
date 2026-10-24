import io
import pytest
pytest.importorskip('numpy')
pytest.importorskip('PIL')
from PIL import Image, ImageDraw
from backend.mark_verification import verify_mark, BoardCameraCalibration, BoundedRetry

PATH=[(.25,.5),(.75,.5)]
CAL=BoardCameraCalibration()


def frame(points=None, fill=25, background=245, obstruction=False):
    image=Image.new('RGB',(640,480),(background,)*3)
    draw=ImageDraw.Draw(image)
    if points: draw.line([CAL.pixel(p) for p in points],fill=(fill,)*3,width=2)
    if obstruction: draw.rectangle((200,140,400,350),fill=(50,)*3)
    buf=io.BytesIO();image.save(buf,format='PNG');return buf.getvalue()


def test_visible_and_missing_marks_use_pixels():
    blank=frame()
    assert verify_mark(blank,frame(PATH),PATH)['outcome']=='visible'
    assert verify_mark(blank,blank,PATH)['outcome']=='missing'


def test_partial_stroke_stops_instead_of_lowering_blindly():
    evidence=verify_mark(frame(),frame([(.25,.5),(.38,.5)]),PATH)
    assert evidence['outcome']=='weak_or_partial'
    assert BoundedRetry().decide(evidence)['action']=='stop_inspect'


def test_occlusion_old_ink_and_scene_changes_are_inconclusive():
    assert verify_mark(frame(),frame(obstruction=True),PATH)['outcome']=='inconclusive'
    assert verify_mark(frame(PATH),frame(PATH),PATH)['outcome']=='inconclusive'
    assert verify_mark(frame(),frame(background=100),PATH)['outcome']=='inconclusive'


def test_unrelated_mark_does_not_confirm_expected_stroke():
    assert verify_mark(frame(),frame([(.25,.7),(.75,.7)]),PATH)['outcome']=='missing'


def test_small_exposure_shift_does_not_count_as_ink():
    assert verify_mark(frame(),frame(background=235),PATH)['outcome']=='missing'


def test_retry_policy_caps_motion_and_stops_on_uncertainty():
    policy=BoundedRetry(step_m=.00025,max_offset_m=.00075,max_retries=3)
    for _ in range(3): assert policy.decide({'outcome':'missing'})['action']=='retry'
    assert policy.decide({'outcome':'missing'})['action']=='stop_limit'
    assert policy.offset_m<=.00075
    assert policy.decide({'outcome':'inconclusive'})['action']=='stop_inspect'
    assert policy.decide({'outcome':'visible'})['action']=='accept'


def test_input_validation():
    with pytest.raises(ValueError): verify_mark(frame(),frame(),[(2,.5),(.7,.5)])
    with pytest.raises(ValueError): verify_mark(frame(),frame(),[(.2,.5)])
    with pytest.raises(ValueError): verify_mark(frame(),frame(),PATH,BoardCameraCalibration(width=800))


def test_faint_mark_is_not_treated_as_verified_success():
    assert verify_mark(frame(),frame(PATH,fill=215),PATH)['outcome']=='weak_or_partial'
    with pytest.raises(ValueError): BoundedRetry(step_m=-.001)
    with pytest.raises(ValueError): BoundedRetry(max_offset_m=.1)


def test_mostly_drawn_stroke_is_still_incomplete():
    assert verify_mark(frame(),frame([(.25,.5),(.65,.5)]),PATH)['outcome']=='weak_or_partial'


def test_batch_changes_are_not_mistaken_for_camera_interference():
    image=Image.new('RGB',(640,480),(245,)*3)
    paths=[[(.2,y),(.8,y)] for y in (.2,.4,.6,.8)]
    draw=ImageDraw.Draw(image)
    for path in paths: draw.line([CAL.pixel(p) for p in path],fill=(25,)*3,width=3)
    buf=io.BytesIO();image.save(buf,format='PNG')
    for path in paths:
        assert verify_mark(frame(),buf.getvalue(),path,CAL,paths)['outcome']=='visible'


def test_partial_mark_localizes_only_missing_section():
    result=verify_mark(frame(),frame([(.25,.5),(.5,.5)]),PATH)
    assert result['outcome']=='weak_or_partial'
    assert len(result['repair_paths'])==1
    repair=result['repair_paths'][0]
    assert .47<repair[0][0]<.53 and repair[-1][0]==pytest.approx(.75)
    assert all(y==pytest.approx(.5) for x,y in repair)
    assert not verify_mark(frame(),frame(PATH,fill=215),PATH)['repair_paths']
