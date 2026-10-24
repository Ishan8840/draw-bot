"""Single-thread-owned MuJoCo dynamics, bounded OpenArm IK, and pen feedback."""
from bisect import bisect_right
from math import hypot
from pathlib import Path
import io
import os
import sys
import numpy as np

from .openarm_ik import OpenArmIK

BOARD_SIZE=.18
BOARD_ORIGIN=np.array([.20,-.07,.14])
PEN_DOWN=.1427
PEN_UP=.175
ROOT=Path(__file__).resolve().parents[2]


def to_world(x,y,z=PEN_DOWN):
    return np.array([BOARD_ORIGIN[0]+BOARD_SIZE*x,BOARD_ORIGIN[1]-BOARD_SIZE*y,z],dtype=float)


def to_board(position):
    return ((float(position[0])-BOARD_ORIGIN[0])/BOARD_SIZE,(BOARD_ORIGIN[1]-float(position[1]))/BOARD_SIZE)


class RobotSimulation:
    def __init__(self):
        # GL selection must precede importing MuJoCo. Physics does not need GL.
        os.environ.setdefault('MUJOCO_GL','egl' if sys.platform.startswith('linux') else 'glfw')
        import mujoco
        self.mj=mujoco
        self.model=mujoco.MjModel.from_xml_path(str(ROOT/'models/openarm/drawing.xml'))
        self.data=mujoco.MjData(self.model)
        self.ik=OpenArmIK(mujoco,self.model)
        self.data.qpos[:]=self.ik.solve(to_world(.5,.5,PEN_UP))
        self.servo_joints=self.data.qpos.copy()
        self.data.ctrl[:]=self.data.qpos
        mujoco.mj_forward(self.model,self.data)
        self.tip_id=self.model.site('pen_tip').id
        self.nib_id=self.model.geom('nib').id
        self.board_id=self.model.geom('board').id
        self.stroke_id=None
        self.phase='idle'
        self.arc=0.
        self.phase_time=0.
        self.target=self.tip.copy()
        self.servo_target=self.target.copy()
        self.xy_correction=np.zeros(2)
        self.renderer=None
        self.last_ink=None
        self.fault=None

    @property
    def tip(self): return self.data.site_xpos[self.tip_id].copy()

    def in_contact(self):
        return any({int(c.geom1),int(c.geom2)}=={self.nib_id,self.board_id} and c.dist<=.00005 for c in self.data.contact)

    def _phase(self,name):
        self.phase=name; self.phase_time=0.

    def _servo(self,target):
        delta=np.asarray(target)-self.servo_target
        self.servo_target+=delta*min(1.,.07*self.model.opt.timestep/max(np.linalg.norm(delta),1e-12))
        compensated=self.servo_target.copy();compensated[:2]+=self.xy_correction
        desired=self.ik.solve(compensated)
        # Bounded command motion prevents discontinuous joint setpoints at travel.
        limits=np.ones(7)*self.model.opt.timestep
        self.servo_joints+=np.clip(desired-self.servo_joints,-limits,limits)
        # Feedforward is a position-servo offset, so the original actuator force
        # limits still bound the TOTAL motor torque. No external joint forces.
        commanded=self.servo_joints+self.data.qfrc_bias/self.model.actuator_gainprm[:,0]
        self.data.ctrl[:]=np.clip(commanded,self.model.actuator_ctrlrange[:,0],self.model.actuator_ctrlrange[:,1])
        self.mj.mj_step(self.model,self.data)
        self.mj.mj_forward(self.model,self.data)
        self.phase_time+=self.model.opt.timestep
        if not np.all(np.isfinite(self.data.qpos)) or self.phase_time>30:
            raise RuntimeError('Simulator tracking timeout or non-finite state')

    @staticmethod
    def at(trajectory,length):
        if length>=trajectory.length: return trajectory.points[-1]
        i=max(0,bisect_right(trajectory.lengths,length)-1)
        if i+1>=len(trajectory.points): return trajectory.points[-1]
        a,b=trajectory.points[i:i+2]
        t=(length-trajectory.lengths[i])/(trajectory.lengths[i+1]-trajectory.lengths[i])
        return type(a)(x=a.x+(b.x-a.x)*t,y=a.y+(b.y-a.y)*t)

    def batch(self,stroke_id,trajectory,speed,seconds):
        if self.stroke_id!=stroke_id:
            self.servo_joints=self.data.qpos.copy()
            self.data.ctrl[:]=self.data.qpos
            self.data.qvel[:]=0  # Simulation stop boundary, not a physical braking model.
            self.servo_target=self.tip.copy();self.xy_correction[:]=0
            self.stroke_id=stroke_id; self.arc=0.; self.last_ink=None
            self.target=self.tip.copy();self.target[2]=PEN_UP
            self._phase('lift')
        samples=[];done=False;fault=None
        for _ in range(max(1,round(seconds/self.model.opt.timestep))):
            start=trajectory.points[0]
            if self.phase=='lift':
                if abs(self.tip[2]-PEN_UP)<.0004:
                    self.target=to_world(start.x,start.y,PEN_UP);self._phase('travel')
            elif self.phase=='travel':
                if np.linalg.norm(self.tip-self.target)<.00025 and np.linalg.norm(self.data.qvel)<.01:
                    self.target=to_world(start.x,start.y,PEN_DOWN);self._phase('lower')
            elif self.phase=='lower':
                if self.in_contact(): self._phase('draw')
            elif self.phase=='draw':
                # Feed target only as measured tracking catches up. Speed is capped
                # at .045 m/s, independently of an excessively high UI speed request.
                if np.linalg.norm(self.tip[:2]-self.target[:2])<.0015:
                    self.arc=min(trajectory.length,self.arc+min(speed,.25)*self.model.opt.timestep)
                p=self.at(trajectory,self.arc)
                self.target=to_world(p.x,p.y)
                if self.arc>=trajectory.length and np.linalg.norm(self.tip[:2]-self.target[:2])<.00008 and np.linalg.norm(self.data.qvel[:2])<.01:
                    self.target[2]=PEN_UP;self._phase('retract')
                # Long curves must not trip the acquisition/settling timeout.
                if self.arc<trajectory.length and np.linalg.norm(self.tip[:2]-self.target[:2])<.002:
                    self.phase_time=0.
            if self.phase=='draw' and self.arc>=trajectory.length:
                # Remove contact-induced endpoint bias without winding up during
                # path motion. The correction is bounded to one millimeter.
                self.xy_correction=np.clip(self.xy_correction+5*self.model.opt.timestep*(self.target[:2]-self.tip[:2]),-.001,.001)
            try:
                self._servo(self.target)
            except Exception as exc:
                fault=f'{type(exc).__name__}: {exc}'
                break
            # Ink is measured geometry ONLY while the nib physically contacts the board.
            # Lift/travel never contributes paths, even after cancelling an old stroke.
            if self.phase in ('lower','draw','retract') and self.in_contact():
                contact=next(c for c in self.data.contact if {int(c.geom1),int(c.geom2)}=={self.nib_id,self.board_id} and c.dist<=.00005)
                x,y=to_board(contact.pos)
                # At an edge MuJoCo reports the midpoint of contacting surfaces;
                # project it onto the finite board within the 3 mm nib radius.
                margin=.003/BOARD_SIZE
                if not (-margin<=x<=1+margin and -margin<=y<=1+margin):
                    fault='Pen left the drawing area'
                    break
                p=(min(1.,max(0.,x)),min(1.,max(0.,y)))
                if self.last_ink is None or hypot(p[0]-self.last_ink[0],p[1]-self.last_ink[1])>.0005:
                    samples.append(p);self.last_ink=p
            if self.phase=='retract' and abs(self.tip[2]-PEN_UP)<.0004:
                done=True;self._phase('idle');break
        # Progress is measured proximity along the planned polyline, not commanded time.
        p=self.at(trajectory,self.arc)
        lag=hypot(to_board(self.tip)[0]-p.x,to_board(self.tip)[1]-p.y)
        reached=max(0.,self.arc-lag)
        return {'samples':samples,'done':done,'reached':reached,'telemetry':self.telemetry(),'fault':fault}

    def reset_execution(self):
        self.stroke_id=None;self.arc=0.;self.last_ink=None;self.fault=None
        self.servo_joints=self.data.qpos.copy()
        self.data.ctrl[:]=self.data.qpos;self.data.qvel[:]=0
        self.target=self.tip.copy();self.servo_target=self.target.copy();self.xy_correction[:]=0;self._phase('idle')
        self.mj.mj_forward(self.model,self.data)
        return self.telemetry()

    def telemetry(self):
        return {'backend':'mujoco','model':'OpenArm v2 right arm: 7 rotary joints, fixed pen','phase':self.phase,
                'simulation_time':float(self.data.time),'joint_positions':self.data.qpos.tolist(),
                'joint_names':[self.model.joint(i).name for i in range(self.model.njnt)],
                'model_source':'enactic/openarm_mujoco@a3c8a57ec15d385e3c1dbd3fd090ee486b7f258c',
                'pen_world_m':self.tip.tolist(),'pen_board':list(to_board(self.tip)),
                'pen_contact':self.in_contact(),'target_world_m':self.target.tolist(),
                'tracking_error_m':float(np.linalg.norm(self.tip[:2]-self.target[:2])),
                'fault':self.fault,'speed_limit_board_units_s':.25,
                'board':{'side_m':BOARD_SIZE,'origin_world_m':BOARD_ORIGIN.tolist(),
                         'x_axis':[1,0,0],'y_axis':[0,-1,0],'normal':[0,0,1]},
                'pause_semantics':'simulation clock frozen; no hardware stop guarantee'}

    def render(self,ink,camera='overview'):
        from PIL import Image
        if self.renderer is None:
            self.renderer=self.mj.Renderer(self.model,height=480,width=640,max_geom=6000)
        cam=self.mj.MjvCamera()
        cam.lookat[:]=[.19,-.12,.31];cam.distance=1.05
        cam.azimuth=90 if camera=='top' else 130
        cam.elevation=-89.9 if camera=='top' else -48
        self.renderer.update_scene(self.data,camera=cam)
        scene=self.renderer.scene
        scene.flags[self.mj.mjtRndFlag.mjRND_SHADOW]=False
        scene.flags[self.mj.mjtRndFlag.mjRND_REFLECTION]=False
        # Segments are display-only feedback from authoritative scene ink.
        # Bound render complexity without changing any persisted trajectories.
        count=sum(max(0,len(points)-1) for _,points in ink)
        stride=max(1,int(np.ceil(count/5000)))
        for user,points in ink:
            selected=points[::stride]
            if points and (not selected or selected[-1]!=points[-1]): selected.append(points[-1])
            for a,b in zip(selected,selected[1:]):
                if scene.ngeom>=scene.maxgeom: break
                geom=scene.geoms[scene.ngeom]
                self.mj.mjv_initGeom(geom,self.mj.mjtGeom.mjGEOM_LINE,np.zeros(3),np.zeros(3),np.eye(3).flatten(),np.array([.15,.4,.65,1] if user else [.08,.12,.16,1]))
                self.mj.mjv_connector(geom,self.mj.mjtGeom.mjGEOM_LINE,2.0,to_world(*a,z=float(BOARD_ORIGIN[2])+.001),to_world(*b,z=float(BOARD_ORIGIN[2])+.001))
                scene.ngeom+=1
        pixels=self.renderer.render()
        output=io.BytesIO();Image.fromarray(pixels).save(output,format='JPEG',quality=82)
        return output.getvalue()

    def close(self):
        if self.renderer is not None: self.renderer.close()
