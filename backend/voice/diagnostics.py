"""Bounded development timings. Never log audio, transcripts or API keys."""
import logging
import time
from collections import deque

log=logging.getLogger('astra.voice')


class Diagnostics:
    def __init__(self, limit=200):
        self.records=deque(maxlen=limit)

    def record(self, stage, stream_id, utterance_id=None, **values):
        entry={'stage':stage,'stream_id':stream_id,'utterance_id':utterance_id,
               'server_wall_ms':round(time.time()*1000,3),'server_monotonic_ms':round(time.perf_counter()*1000,3),**values}
        self.records.append(entry)
        log.info('voice_latency %s',entry)
        return entry

    def snapshot(self):
        return list(self.records)
