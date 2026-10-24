import base64
import io
import pytest
pytest.importorskip('PIL')
from PIL import Image
from backend.board_observation import image_content
from backend.models import Scene, DrawingObject, StrokeState


def test_image_shows_only_confirmed_ink_in_normalized_frame():
    stroke=StrokeState(stroke_id='line',geometry={'type':'line','start':{'x':.1,'y':.5},'end':{'x':.9,'y':.5}},
                       total_length=.8,executed_points=[{'x':.1,'y':.5},{'x':.4,'y':.5}],progress=.375)
    scene=Scene(objects=[DrawingObject(object_id='sketch',source='user',generation=0,strokes=[stroke])])
    content=image_content(scene)
    image=Image.open(io.BytesIO(base64.b64decode(content['image_url'].split(',')[1])))
    assert image.getpixel((150,384))==(39,104,148)
    assert image.getpixel((600,384))==(255,255,255)  # Unexecuted intent is invisible.
    assert content['type']=='input_image' and content['detail']=='high'
