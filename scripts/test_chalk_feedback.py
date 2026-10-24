"""Image-only chalk feedback experiment using real MuJoCo motion and shared tools.

python -m scripts.test_chalk_feedback [--sandbox-heartbeat]
No OpenAI key needed. Does not modify the running application's scene or policy.
"""
import argparse
import asyncio
import json
from html import escape
from pathlib import Path
import tempfile
import time
from backend.drawing_engine import DrawingEngine
from backend.models import StrokeSpec
from backend.geometry import flatten
from backend.scene_state import SceneStateManager
from backend.websocket_manager import WebSocketManager
from backend.simulation.executor import MuJoCoDrawingExecutor
from backend.mark_verification import verify_mark, BoundedRetry


async def run_case(name, loss, wear_rate, output):
    output.mkdir(parents=True,exist_ok=True)
    attempts=[]
    with tempfile.TemporaryDirectory(prefix='astra-chalk-') as temporary:
        ex=MuJoCoDrawingExecutor(SceneStateManager(str(Path(temporary)/'scene.sqlite3')),WebSocketManager())
        engine=DrawingEngine(ex)
        await ex.initialize()
        try:
            # Environment setup. The decision policy never receives these values.
            await ex._call(ex.robot.configure_chalk,loss,wear_rate)
            policy=BoundedRetry(step_m=.00025,max_offset_m=.00075,max_retries=3)
            for number in range(4):
                if not await ex.prepare_observation(): raise RuntimeError('Could not obtain clear pre-draw view')
                before=await ex.frame('top',fresh=True)
                (output/f'{number}-before.jpg').write_bytes(before)
                stroke=StrokeSpec(stroke_id=f'probe-{number}',geometry={
                    'type':'line','start':{'x':.25,'y':.5},'end':{'x':.75,'y':.5}})
                points=[(p.x,p.y) for p in flatten(stroke.geometry).points]
                started=time.perf_counter()
                await engine.draw_strokes(f'probe-attempt-{number}',[stroke])
                for _ in range(1000):
                    await ex.advance(.05)
                    if ex.telemetry.get('fault'): raise RuntimeError(ex.telemetry['fault'])
                    if ex.state.scene.status=='idle': break
                else: raise RuntimeError('Motion did not complete')
                if not await ex.prepare_observation(): raise RuntimeError('Could not obtain clear post-draw view')
                after=await ex.frame('top',fresh=True)
                (output/f'{number}-after.jpg').write_bytes(after)
                evidence=verify_mark(before,after,points)
                decision=policy.decide(evidence)
                attempts.append({'attempt':number,'evidence':evidence,'decision':decision,
                                 'motion_and_observation_wall_s':time.perf_counter()-started})
                print(name,number,evidence['outcome'],decision['action'],flush=True)
                if decision['action']!='retry': break
                # This simulation-only experiment allows <=0.75 mm total correction.
                # Production Astra has no depth-control tool and never sees the loss.
                await ex._call(ex.robot.set_depth_offset,decision['offset_m'])
            report={'case':name,'attempts':attempts,'final_action':attempts[-1]['decision']['action'],
                    'environment_setup':{'initial_loss_m':loss,'wear_m_per_m':wear_rate},
                    'detector_inputs':'before/after camera JPEGs, intended line, fixed camera calibration only'}
            (output/'result.json').write_text(json.dumps(report,indent=2))
            (output/'scene.json').write_text(ex.state.scene.model_dump_json(indent=2))
            return report
        finally: await ex.close()


def write_report(output, reports):
    cards=[]
    for report in reports:
        rows=[]
        for attempt in report['attempts']:
            n=attempt['attempt']; e=attempt['evidence']; d=attempt['decision']
            prefix=f"{report['case']}/{n}"
            rows.append(f'<article><h3>Attempt {n+1}: {escape(e["outcome"])} → {escape(d["action"])}</h3>'
                        f'<p>Visible path coverage: {e["path_coverage"]:.0%}; motion + inspection: {attempt["motion_and_observation_wall_s"]:.2f} s</p>'
                        f'<div class="frames"><figure><img src="{prefix}-before.jpg"><figcaption>Before</figcaption></figure>'
                        f'<figure><img src="{prefix}-after.jpg"><figcaption>After</figcaption></figure></div></article>')
        cards.append(f'<section><h2>{escape(report["case"])}: {escape(report["final_action"])}</h2>'+''.join(rows)+'</section>')
    (output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>Chalk feedback experiment</title>'
        '<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 16px;background:#eef2f2;color:#172829}'
        'article{background:white;padding:16px;margin:12px 0;border-radius:8px}.frames{display:flex;flex-wrap:wrap}'
        'figure{flex:1;min-width:250px;margin:8px}img{width:100%}figcaption{font-weight:600}</style>'
        '<h1>Camera-only chalk feedback</h1><p>Real MuJoCo motion; simplified chalk shortening. Decisions use camera pixels and the intended path only. '
        'Corrections are simulation-only, capped at 0.75 mm. These results do not establish hardware safety or accuracy.</p>'+''.join(cards))


async def main(args):
    output=Path(args.output)
    reports=[]
    for name,loss,rate in [('fresh',0.,0.),('shortened',.0006,0.),('exhausted',.003,0.),('wearing',0.,.02)]:
        reports.append(await run_case(name,loss,rate,output/name))
    (output/'results.json').write_text(json.dumps(reports,indent=2))
    write_report(output,reports)
    expected={'fresh':'accept','shortened':'accept','exhausted':'stop_limit','wearing':'stop_inspect'}
    for report in reports:
        if report['final_action']!=expected[report['case']]:
            raise RuntimeError(f"Unexpected result in {report['case']}: {report['final_action']}")
    print(f'Passed four physical simulation cases. Artifacts: {output.resolve()}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='artifacts/chalk-feedback')
    parser.add_argument('--sandbox-heartbeat',action='store_true')
    args=parser.parse_args()
    if args.sandbox_heartbeat:
        from scripts.test_sandbox import PollingPolicy
        asyncio.set_event_loop_policy(PollingPolicy())
    asyncio.run(main(args))
