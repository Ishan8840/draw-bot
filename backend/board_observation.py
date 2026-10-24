"""Raster observation of confirmed ink; no task recognition or extra model."""
import base64
import io


def image_content(scene):
    from PIL import Image, ImageDraw
    image=Image.new('RGB',(768,768),'white')
    draw=ImageDraw.Draw(image)
    for obj in scene.objects:
        for stroke in obj.strokes:
            points=[(p.x*767,p.y*767) for p in stroke.executed_points]
            if len(points)>1:
                draw.line(points,fill=(39,104,148) if obj.source=='user' else (25,35,38),width=2)
            elif points:
                x,y=points[0];draw.ellipse((x-1,y-1,x+1,y+1),fill=(25,35,38))
    output=io.BytesIO();image.save(output,format='PNG')
    return {'type':'input_image','detail':'high','image_url':'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode('ascii')}
