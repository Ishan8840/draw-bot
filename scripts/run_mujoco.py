"""Start the existing app with the OpenArm MuJoCo executor: python -m scripts.run_mujoco."""
import argparse
import os
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--database',default=str(Path(__file__).resolve().parents[1]/'data/openarm-scene.sqlite3'))
    args=parser.parse_args()
    os.environ['ASTRA_EXECUTOR']='mujoco'
    os.environ['ASTRA_SCENE_DB']=args.database
    import uvicorn
    uvicorn.run('backend.main:app',host=args.host,port=args.port,workers=1)


if __name__=='__main__': main()
