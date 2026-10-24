"""Start the existing app with the OpenArm MuJoCo executor: python -m scripts.run_mujoco."""
import argparse
import os
import math
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--database',default=str(Path(__file__).resolve().parents[1]/'data/openarm-scene.sqlite3'))
    parser.add_argument('--chalk-wear-rate',type=float,default=None,
                        help='Meters of tip shortening per meter drawn (default: environment setting, otherwise 0.002; 0 disables wear)')
    args=parser.parse_args()
    try:
        rate=args.chalk_wear_rate if args.chalk_wear_rate is not None else float(os.getenv('SIM_CHALK_WEAR_RATE','0.002'))
    except ValueError:
        parser.error('SIM_CHALK_WEAR_RATE must be a number')
    if not math.isfinite(rate) or not 0<=rate<=.1:
        parser.error('Chalk wear rate must be finite and between 0 and 0.1')
    os.environ['SIM_CHALK_WEAR_RATE']=str(rate)
    print(f'Simulated chalk wear: {rate*1000:g} mm per meter drawn' if rate else 'Simulated chalk wear: disabled',flush=True)
    os.environ['ASTRA_EXECUTOR']='mujoco'
    os.environ['ASTRA_SCENE_DB']=args.database
    import uvicorn
    uvicorn.run('backend.main:app',host=args.host,port=args.port,workers=1)


if __name__=='__main__': main()
