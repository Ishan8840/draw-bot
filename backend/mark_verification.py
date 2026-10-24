"""Fixed-camera, image-only evidence of new marks near an intended path.

Inputs contain pixels, intended geometry, and a camera calibration; no simulator
state/contact/ink buffers. Results are evidence, never proof of chalk contact.
"""
from dataclasses import dataclass, asdict
import io
import numpy as np
from PIL import Image, ImageDraw, ImageFilter


@dataclass(frozen=True)
class BoardCameraCalibration:
    # Calibrated board corners for this fixed, rectified 640x480 overhead view.
    # Real images must be undistorted/rectified before using this simple mapping.
    left: float = 166.6
    top: float = 86.6
    right: float = 473.4
    bottom: float = 393.4
    width: int = 640
    height: int = 480

    def __post_init__(self):
        if not (0<=self.left<self.right<self.width and 0<=self.top<self.bottom<self.height):
            raise ValueError('Invalid board camera calibration')

    def pixel(self, point):
        x,y=point
        if not (0<=x<=1 and 0<=y<=1):
            raise ValueError('Expected path is outside the calibrated board')
        return (self.left+x*(self.right-self.left), self.top+y*(self.bottom-self.top))


def verify_mark(before, after, expected_points, calibration=BoardCameraCalibration(), batch_paths=None):
    """Compare clear, stationary camera frames around one intended stroke."""
    def decode(data):
        frame=Image.open(io.BytesIO(data)).convert('L')
        if frame.size!=(calibration.width,calibration.height):
            raise ValueError('Frame size does not match camera calibration')
        return np.asarray(frame,dtype=float)
    a,b=decode(before),decode(after)
    pixels=[calibration.pixel(p) for p in expected_points]
    if len(pixels)<2: raise ValueError('Verification requires a path with at least two points')
    mask=Image.new('L',(calibration.width,calibration.height))
    ImageDraw.Draw(mask).line(pixels,fill=255,width=9)
    roi=np.asarray(mask)>0
    board=np.zeros(a.shape,dtype=bool)
    board[int(calibration.top)+4:int(calibration.bottom)-4,int(calibration.left)+4:int(calibration.right)-4]=True
    # Other planned strokes in this pass are expected changes, not interference.
    expected_mask=mask.copy()
    if batch_paths:
        brush=ImageDraw.Draw(expected_mask)
        for path in batch_paths:
            brush.line([calibration.pixel(p) for p in path],fill=255,width=9)
    outside=board & ~(np.asarray(expected_mask)>0)
    if outside.sum()<100: raise ValueError('Insufficient reference background')
    shift=float(np.median((b-a)[outside]))
    change=np.abs(b-a-shift)>20
    outside_fraction=float(np.mean(change[outside]))
    # Older ink on this path makes before/after evidence ambiguous (overdrawing).
    background=float(np.median(a[board]))
    prior_fraction=float(np.mean(np.abs(a[roi]-background)>40))
    new_pixels=int(np.sum(change & roi))
    nearby=np.asarray(Image.fromarray((change*255).astype('uint8')).filter(ImageFilter.MaxFilter(7)))>0
    samples=[]
    sample_points=[]
    for start,end in zip(pixels,pixels[1:]):
        length=np.hypot(end[0]-start[0],end[1]-start[1])
        for t in np.linspace(0,1,max(2,int(length/2))):
            x,y=np.asarray(start)+(np.asarray(end)-start)*t
            samples.append(bool(nearby[round(y),round(x)]))
            sample_points.append(((x-calibration.left)/(calibration.right-calibration.left),
                                  (y-calibration.top)/(calibration.bottom-calibration.top)))
    coverage=float(np.mean(samples))
    if abs(shift)>25 or outside_fraction>.02 or prior_fraction>.18:
        outcome='inconclusive'
    elif coverage>=.95 and new_pixels>=12:
        contrast=float(np.median(np.abs(b-a-shift)[change & roi]))
        outcome='visible' if contrast>=50 else 'weak_or_partial'
    elif coverage<.1 and new_pixels<12:
        outcome='missing'
    else:
        outcome='weak_or_partial'
    # Only localize clear gaps with stable frames and strong visible evidence.
    # Faint/ambiguous strokes never authorize automatic tracing or lowering.
    repairs=[]
    contrast=float(np.median(np.abs(b-a-shift)[change & roi])) if new_pixels else 0.
    if outcome=='weak_or_partial' and contrast>=50 and prior_fraction<=.18 and outside_fraction<=.02 and abs(shift)<=25:
        start=None
        for index,visible in enumerate(samples+[True]):
            if not visible and start is None:
                start=index
            elif visible and start is not None:
                if index-start>=4:
                    # Small overlap at each end joins the repair to visible ink.
                    repairs.append(sample_points[max(0,start-2):min(len(samples),index+2)])
                start=None
        if len(repairs)>8 or any(len(path)>4096 for path in repairs): repairs=[]
    return {'outcome':outcome,'repair_paths':repairs,'path_coverage':coverage,'new_pixels':new_pixels,
            'outside_change_fraction':outside_fraction,'prior_mark_fraction':prior_fraction,
            'brightness_shift':shift,'source':'camera_frame_difference',
            'note':'Evidence of changed pixels near the planned path, not a contact or force measurement.'}


@dataclass
class BoundedRetry:
    """Experiment-only policy: ambiguity stops; small missing-mark corrections cap."""
    step_m: float = .0002
    max_offset_m: float = .001
    max_retries: int = 5
    retries: int = 0
    offset_m: float = 0.

    def __post_init__(self):
        if not (0<self.step_m<=self.max_offset_m<=.001 and 0<=self.max_retries<=5):
            raise ValueError('Invalid bounded simulation retry policy')
        if self.retries!=0 or self.offset_m!=0:
            raise ValueError('Retry policy must start without corrections')

    def decide(self, evidence):
        if evidence['outcome']=='visible': return {'action':'accept',**asdict(self)}
        if evidence['outcome']!='missing': return {'action':'stop_inspect',**asdict(self)}
        if self.retries>=self.max_retries or self.offset_m+self.step_m>self.max_offset_m+1e-12:
            return {'action':'stop_limit',**asdict(self)}
        self.retries+=1
        self.offset_m+=self.step_m
        return {'action':'retry',**asdict(self)}
