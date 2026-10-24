import re
from difflib import SequenceMatcher


def normalize(text):
    return ' '.join(re.findall(r"[a-z0-9']+", text.lower()))


def classify_control(text, phrases):
    text=normalize(text)
    text=re.sub(r'^(?:hey )?astra\s+', '', text)
    text=re.sub(r'^please\s+', '', text)
    # Repeated urgency is common: "wait, stop!". Negations do not match.
    words=text.split()
    if words and all(w in {'wait','stop','pause','please'} for w in words):
        return 'pause', True
    for phrase in sorted(phrases,key=len,reverse=True):
        phrase=normalize(phrase)
        if text==phrase or text.startswith(phrase+' '):
            action='cancel' if phrase.startswith('cancel') else 'pause'
            remainder=text[len(phrase):].strip()
            return action, remainder in ('','please','now','drawing','it')
    return None,False


def spoken_text(text):
    """Deterministic presentation filter, never an action parser."""
    text=re.sub(r'```.*?```',' ',text,flags=re.S)
    text=re.sub(r'`[^`]*`',' ',text)
    text=re.sub(r'\[([^]]+)\]\([^)]*\)',r'\1',text)
    lines=[line.strip(' #*-') for line in text.splitlines() if not any(k in line for k in ('{"','stroke_id','object_id','<svg','executed_points'))]
    text=' '.join(lines)
    sentences=re.split(r'(?<=[.!?])\s+',text)
    text=' '.join(sentences[:2]).strip()[:300]
    return text


def likely_echo(text, last_spoken):
    a,b=normalize(text),normalize(last_spoken)
    return len(a)>12 and bool(b) and SequenceMatcher(None,a,b).ratio()>.9
