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


OBSERVATION_POSE=to_world(-.7,.5,.19)


class RobotSimulation:
    def __init__(self):
        # GL selection must precede importing MuJoCo. Physics does not need GL.
        os.environ.setdefault('MUJOCO_GL','egl' if sys.platform.startswith('linux') else 'glfw')
        import mujoco
        self.mj=mujoco
        self.model=mujoco.MjModel.from_xml_path(str(ROOT/'models/openarm/drawing.xml'))
        self.data=mujoco.MjData(self.model)
        self.ik=OpenArmIK(mujoco,self.model)
        self.feedback=mujoco.MjData(self.model)
        self.world_ink={}  # Environment-only marks, never controller observations.
        self.data.qpos[:]=self.ik.solve(OBSERVATION_POSE)
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
        self.reacquire=False
        self.chalk_loss_m=0.
        self.chalk_wear_rate=0.
        self.depth_offset_m=0.
        self._nib_position=self.model.geom_pos[self.nib_id].copy()

    @property
    def draw_height(self):
        return PEN_DOWN-self.depth_offset_m

    def set_depth_offset(self, offset_m):
        if not np.isfinite(offset_m) or not 0<=offset_m<=.001:
            raise ValueError('Simulation experiment correction must be between 0 and 1 mm')
        self.depth_offset_m=float(offset_m)

    def configure_chalk(self, loss_m=0., wear_rate=0.):
        """Environment fault injection ONLY, unavailable to Astra/controller sensors.

        Shift the physical nib back along the chalk axis without changing the
        nominal tool site used for FK. This approximates shortening, not fracture,
        dust, pressure-dependent abrasion, or realistic chalk material mechanics.
        """
        if not np.isfinite(loss_m) or not 0<=loss_m<=.01 or not np.isfinite(wear_rate) or not 0<=wear_rate<=.1:
            raise ValueError('Invalid simulated chalk wear')
        self.chalk_loss_m=float(loss_m);self.chalk_wear_rate=float(wear_rate)
        self.model.geom_pos[self.nib_id]=self._nib_position+[loss_m,0,0]

    @property
    def tip(self):
        # Encoder measurements + nominal robot model; no simulator site feedback.
        self.feedback.qpos[:]=self.data.qpos
        self.feedback.qvel[:]=self.data.qvel
        self.mj.mj_forward(self.model,self.feedback)
        return self.feedback.site_xpos[self.tip_id].copy()

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
        self.feedback.qpos[:]=self.data.qpos
        self.feedback.qvel[:]=self.data.qvel
        self.mj.mj_forward(self.model,self.feedback)
        commanded=self.servo_joints+self.feedback.qfrc_bias/self.model.actuator_gainprm[:,0]
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
        if self.reacquire:
            self.servo_target=self.tip.copy();self.xy_correction[:]=0
            self.target=self.tip.copy();self.target[2]=PEN_UP
            self._phase('lift');self.reacquire=False
        samples=[];done=False;fault=None
        for _ in range(max(1,round(seconds/self.model.opt.timestep))):
            start=self.at(trajectory,self.arc)
            if self.phase=='lift':
                if abs(self.tip[2]-PEN_UP)<.0004:
                    self.target=to_world(start.x,start.y,PEN_UP);self._phase('travel')
            elif self.phase=='travel':
                if np.linalg.norm(self.tip-self.target)<.00025 and np.linalg.norm(self.data.qvel)<.01:
                    self.target=to_world(start.x,start.y,self.draw_height);self._phase('lower')
            elif self.phase=='lower':
                if abs(self.tip[2]-self.draw_height)<.0006: self._phase('draw')
            elif self.phase=='draw':
                # Feed target only as measured tracking catches up. Speed is capped
                # at .045 m/s, independently of an excessively high UI speed request.
                if np.linalg.norm(self.tip[:2]-self.target[:2])<.0015:
                    self.arc=min(trajectory.length,self.arc+min(speed,.25)*self.model.opt.timestep)
                p=self.at(trajectory,self.arc)
                self.target=to_world(p.x,p.y,self.draw_height)
                if self.arc>=trajectory.length and np.linalg.norm(self.tip[:2]-self.target[:2])<.00008 and np.linalg.norm(self.data.qvel[:2])<.01:
                    self.target[2]=PEN_UP;self._phase('retract')
                # Long curves must not trip the acquisition/settling timeout.
                if self.arc<trajectory.length and np.linalg.norm(self.tip[:2]-self.target[:2])<.002:
                    self.phase_time=0.
            if (self.phase=='draw' and self.arc>=trajectory.length) or (self.phase=='travel' and np.linalg.norm(self.tip-self.target)<.002):
                # Remove contact-induced endpoint bias without winding up during
                # path motion. The correction is bounded to one millimeter.
                self.xy_correction=np.clip(self.xy_correction+5*self.model.opt.timestep*(self.target[:2]-self.tip[:2]),-.001,.001)
            try:
                self._servo(self.target)
            except Exception as exc:
                fault=f'{type(exc).__name__}: {exc}'
                break
            # Hidden environment physics deposits ink. Its output is used ONLY
            # by the camera renderer, never by motion control or scene progress.
            self._deposit_world_ink(stroke_id)
            # Expected trace from encoders and calibrated board, NOT sensed ink.
            if self.phase in ('lower','draw','retract') and abs(self.tip[2]-self.draw_height)<.0006:
                x,y=to_board(self.tip)
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

    def observation_batch(self, seconds):
        """Lift then park via bounded actuators; retain the interrupted path cursor."""
        if self.phase not in ('observe_lift','observe_park'):
            if np.linalg.norm(self.tip-OBSERVATION_POSE)<.0004:
                return True
            self.reacquire=self.stroke_id is not None
            self.xy_correction[:]=0
            self.servo_target=self.tip.copy()
            self.target=self.tip.copy();self.target[2]=OBSERVATION_POSE[2]
            self._phase('observe_lift')
        for _ in range(max(1,round(seconds/self.model.opt.timestep))):
            if self.phase=='observe_lift' and abs(self.tip[2]-OBSERVATION_POSE[2])<.0004:
                self.target=OBSERVATION_POSE.copy();self._phase('observe_park')
            self._servo(self.target)
            # The environment may mark the very beginning of a pen lift.
            if self.stroke_id:
                self._deposit_world_ink(self.stroke_id, observation_lift=self.phase=='observe_lift')
            if self.phase=='observe_park' and np.linalg.norm(self.tip-OBSERVATION_POSE)<.0004 and np.linalg.norm(self.data.qvel)<.01:
                self._phase('idle')
                return True
        return False

    def _deposit_world_ink(self, stroke_id, observation_lift=False):
        """Simulator environment only: a real camera would see these marks."""
        if not observation_lift and self.phase not in ('lower','draw','retract'):
            return
        for contact in self.data.contact:
            if {int(contact.geom1),int(contact.geom2)}=={self.nib_id,self.board_id} and contact.dist<=.00005:
                x,y=to_board(contact.pos)
                p=(min(1.,max(0.,x)),min(1.,max(0.,y)))
                points=self.world_ink.setdefault(stroke_id,[])
                if not points or hypot(p[0]-points[-1][0],p[1]-points[-1][1])>.0005:
                    distance=hypot(p[0]-points[-1][0],p[1]-points[-1][1])*BOARD_SIZE if points else 0.
                    points.append(p)
                    if self.chalk_wear_rate:
                        self.configure_chalk(min(.01,self.chalk_loss_m+distance*self.chalk_wear_rate),self.chalk_wear_rate)
                break

    def reset_execution(self):
        self.stroke_id=None;self.arc=0.;self.last_ink=None;self.fault=None;self.reacquire=False
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
                'estimated_pen_world_m':self.tip.tolist(),'estimated_pen_board':list(to_board(self.tip)),
                'joint_velocities':self.data.qvel.tolist(),'feedback_source':'encoders + nominal forward kinematics',
                'ink_verified':False,'target_world_m':self.target.tolist(),
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
        cam.elevation=-90 if camera=='top' else -48
        if camera=='top':
            cam.lookat[:]=[.29,-.16,.14];cam.distance=.34
        self.renderer.update_scene(self.data,camera=cam)
        scene=self.renderer.scene
        scene.flags[self.mj.mjtRndFlag.mjRND_SHADOW]=False
        scene.flags[self.mj.mjtRndFlag.mjRND_REFLECTION]=False
        # Environment marks are visible through the camera, not controller feedback.
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
