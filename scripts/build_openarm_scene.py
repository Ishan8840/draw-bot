"""Vendor a drawing fixture derived from the pinned official OpenArm v2 MJCF.

python -m scripts.build_openarm_scene --upstream /path/to/openarm_mujoco
"""
import argparse
from copy import deepcopy
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]


def build(upstream):
    root=ET.parse(upstream/'v2/openarm_bimanual.xml').getroot()
    root.set('model','OpenArm_v2_drawing')
    root.find('option').set('timestep','0.002')
    world=root.find('worldbody')
    right=deepcopy(world.find("body[@name='openarm_right_base_link']"))
    world.clear();world.append(right)
    right.set('pos','0 -.03 .55')
    # The gripper is rigidly clamped around the pen in this fixture; no extra
    # prismatic plotting axes are introduced. All seven arm joints are original.
    for body in right.iter('body'):
        for joint in list(body.findall('joint')):
            if 'finger' in joint.get('name',''): body.remove(joint)
    root.remove(root.find('equality'))
    actuators=root.find('actuator')
    for actuator in list(actuators):
        if not actuator.get('name','').startswith('right_joint'): actuators.remove(actuator)
        else:
            number=int(actuator.get('name').split('joint')[1].split('_')[0])
            actuator.set('kp',str(1800 if number<=4 else 500))
            actuator.set('kv',str(45 if number<=4 else 12))
    ee=right.find(".//body[@name='openarm_right_ee_base_link']")
    ET.SubElement(ee,'geom',name='pen_barrel',type='capsule',fromto='.035 0 -.10 -.125 0 -.10',size='.005',mass='.015',rgba='.9 .6 .12 1',contype='0',conaffinity='0')
    ET.SubElement(ee,'geom',name='nib',type='sphere',pos='-.13 0 -.10',size='.003',mass='.002',rgba='.08 .1 .12 1',contype='1',conaffinity='1',friction='.03 .001 .001',solref='.004 1')
    ET.SubElement(ee,'site',name='pen_tip',pos='-.13 0 -.10',size='.001',rgba='1 .2 .1 1',group='4')
    ET.SubElement(world,'light',pos='.4 -.2 1.8',diffuse='.9 .9 .9')
    ET.SubElement(world,'geom',name='floor',type='plane',size='2 2 .01',rgba='.19 .24 .25 1',contype='0',conaffinity='0')
    ET.SubElement(world,'geom',name='mount',type='cylinder',pos='0 -.03 .26',size='.065 .26',rgba='.28 .30 .32 1',contype='0',conaffinity='0')
    ET.SubElement(world,'geom',name='board_frame',type='box',pos='.29 -.16 .125',size='.11 .11 .012',rgba='.42 .49 .48 1',contype='0',conaffinity='0')
    ET.SubElement(world,'geom',name='board',type='box',pos='.29 -.16 .13',size='.09 .09 .01',rgba='.99 .99 .97 1',contype='1',conaffinity='1',friction='.03 .001 .001')
    visual=ET.SubElement(root,'visual')
    ET.SubElement(visual,'global',offwidth='800',offheight='600')
    ET.SubElement(visual,'headlight',diffuse='.8 .8 .8',ambient='.4 .4 .4')
    used={g.get('mesh') for g in world.iter('geom') if g.get('mesh')}
    target=ROOT/'models/openarm';target.mkdir(parents=True,exist_ok=True)
    asset=root.find('asset')
    for mesh in list(asset.findall('mesh')):
        if mesh.get('name') not in used: asset.remove(mesh);continue
        path=Path(mesh.get('file'))
        destination=target/'assets'/path;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(upstream/'v2/assets'/path,destination)
    shutil.copy2(upstream/'LICENSE',target/'LICENSE')
    ET.indent(root)
    (target/'drawing.xml').write_text('<!-- Derived from Enactic OpenArm v2, Copyright 2026 Enactic, Inc.; Apache-2.0. See LICENSE and SOURCE.md. Modified drawing fixture and gains. -->\n'+ET.tostring(root,encoding='unicode')+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--upstream',type=Path,required=True)
    build(parser.parse_args().upstream)
