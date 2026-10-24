"""Bounded damped-Jacobian IK for the actual seven-joint OpenArm model.

Only a scratch MjData is changed by IK. Dynamics uses position actuators; it is
never replaced by kinematic teleportation during drawing.
"""
import numpy as np

# Fixed marker is clamped transverse to the gripper. This wrist orientation
# places the marker's local -X axis along world -Z (normal to the board).
PEN_ROTATION=np.array([[0.,0.,-1.],[0.,1.,0.],[1.,0.,0.]])
HOME_SEED=np.array([.3,.1,0.,1.,0.,-.2,0.])


class OpenArmIK:
    def __init__(self,mj,model):
        self.mj,self.model=mj,model
        self.data=mj.MjData(model)
        self.site=model.site('pen_tip').id
        self.lower=model.jnt_range[:,0]+1e-5
        self.upper=model.jnt_range[:,1]-1e-5
        self.q=np.clip(HOME_SEED,self.lower,self.upper)
        self.jp=np.zeros((3,model.nv));self.jr=np.zeros((3,model.nv))

    def solve(self,position,seed=None,max_iterations=120):
        q=np.clip(self.q if seed is None else seed,self.lower,self.upper).copy()
        for _ in range(max_iterations):
            self.data.qpos[:]=q
            self.mj.mj_kinematics(self.model,self.data)
            self.mj.mj_comPos(self.model,self.data)
            rotation=self.data.site_xmat[self.site].reshape(3,3)
            dp=np.asarray(position)-self.data.site_xpos[self.site]
            dr=sum(np.cross(rotation[:,i],PEN_ROTATION[:,i]) for i in range(3))*.5
            if np.linalg.norm(dp)<1e-5 and np.linalg.norm(dr)<1e-4:
                self.q=q
                return q.copy()
            self.mj.mj_jacSite(self.model,self.data,self.jp,self.jr,self.site)
            jac=np.vstack((self.jp,.15*self.jr))
            error=np.r_[dp,.15*dr]
            delta=jac.T@np.linalg.solve(jac@jac.T+1e-6*np.eye(6),error)
            delta*=min(1.,.12/max(np.max(np.abs(delta)),1e-12))
            q=np.clip(q+delta,self.lower,self.upper)
        raise ValueError(f'OpenArm target is unreachable within joint/orientation limits (position error {np.linalg.norm(dp):.6f} m)')
