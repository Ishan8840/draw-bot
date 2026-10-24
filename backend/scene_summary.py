"""Bounded spatial context, with exact geometry accessible by paginated tools."""
from .geometry import flatten


def bounds(points):
    if not points:
        return None
    return {'x_min':min(p.x for p in points), 'y_min':min(p.y for p in points),
            'x_max':max(p.x for p in points), 'y_max':max(p.y for p in points)}


def object_bounds(obj, ink_only=False):
    points = []
    for stroke in obj.strokes:
        if ink_only:
            points.extend(stroke.executed_points)
        elif stroke.status != 'cancelled':
            points.extend(flatten(stroke.geometry).points)
        else:
            points.extend(stroke.executed_points)
    return bounds(points)


def summary(scene, offset=0, limit=50):
    objects = []
    occupied = []
    for index, obj in enumerate(scene.objects):
        bbox = object_bounds(obj)
        if bbox:
            occupied.append(bbox)
        if offset <= index < offset+limit:
            objects.append({'object_id':obj.object_id, 'source':obj.source, 'bounds':bbox,
                            'ink_bounds':object_bounds(obj, True), 'stroke_count':len(obj.strokes),
                            'strokes':[{'stroke_id':s.stroke_id,'status':s.status,'progress':round(s.progress,4),
                                        'geometry_type':s.geometry.type,'has_ink':bool(s.executed_points)} for s in obj.strokes[:64]],
                            'strokes_truncated':len(obj.strokes)>64})
    # Conservative free rectangles on an 8x8 occupancy grid; not a packing solver.
    free=[]
    for row in range(8):
        for col in range(8):
            x,y=col/8,row/8
            if not any(b['x_min'] <= x+.125 and b['x_max'] >= x and b['y_min'] <= y+.125 and b['y_max'] >= y for b in occupied):
                free.append({'x':x,'y':y,'width':.125,'height':.125})
    return {'revision':scene.revision,'generation':scene.generation,'status':scene.status,'speed':scene.speed,
            'current_stroke_id':scene.current_stroke_id,'pending_count':len(scene.pending_stroke_ids),
            'completed_count':len(scene.completed_stroke_ids),'cancelled_count':len(scene.cancelled_stroke_ids),
            'board':{'x':[0,1],'y':[0,1],'aspect_ratio':1},'objects':objects,'object_count':len(scene.objects),
            'next_offset':offset+limit if offset+limit<len(scene.objects) else None,
            'available_area':{'method':'conservative 8x8 cells excluding object bounds','cells':free}}


def detailed_object(obj, offset, limit):
    # Each page includes exact intended geometry and exact executed ink. Bound page
    # size by points, in addition to the requested stroke count.
    selected=[]
    points=0
    for stroke in obj.strokes[offset:offset+limit]:
        count=len(stroke.executed_points)+len(getattr(stroke.geometry,'points',[]))
        if selected and points+count>10000:
            break
        selected.append(stroke.model_dump(mode='json'))
        points+=count
    next_offset=offset+len(selected)
    return {'object_id':obj.object_id,'source':obj.source,'strokes':selected,
            'stroke_count':len(obj.strokes),'next_offset':next_offset if next_offset<len(obj.strokes) else None}


def recent_user_ink(scene):
    """Activity-independent observation, not symbol recognition or rule inference."""
    objects=[]
    for obj in [o for o in scene.objects if o.source=='user'][-8:]:
        strokes=[]
        for stroke in obj.strokes[-4:]:
            points=stroke.executed_points
            sampled=points if len(points)<=64 else [points[round(i*(len(points)-1)/63)] for i in range(64)]
            strokes.append({'stroke_id':stroke.stroke_id,'executed_points':[p.model_dump() for p in sampled],
                            'downsampled':len(points)>64,'point_count':len(points)})
        objects.append({'object_id':obj.object_id,'strokes':strokes,'strokes_truncated':len(obj.strokes)>4})
    return {'objects':objects,'note':'Latest 8 user objects, last 4 strokes each, up to 64 points per stroke. Retrieve exact geometry through get_scene_state.'}
