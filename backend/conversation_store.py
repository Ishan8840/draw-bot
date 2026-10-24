"""Local conversation journal; model continuations are bounded to a single turn.

A receipt is reserved before a tool is run. An unresolved receipt is never replayed
following a crash (at-most-once admission, not a distributed exactly-once claim).
"""
import asyncio
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4
from .agent_models import ChatSession


class ConversationStore:
    def __init__(self, database):
        if database != ':memory:':
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.listeners = set()
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, status TEXT NOT NULL, turn_id TEXT);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, turn_id TEXT,
          kind TEXT NOT NULL, content TEXT NOT NULL, tool_name TEXT, call_id TEXT, created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
        CREATE TABLE IF NOT EXISTS requests (
          session_id TEXT NOT NULL, request_id TEXT NOT NULL, text TEXT NOT NULL, turn_id TEXT NOT NULL,
          PRIMARY KEY(session_id, request_id));
        CREATE TABLE IF NOT EXISTS receipts (
          turn_id TEXT NOT NULL, call_id TEXT NOT NULL, signature TEXT NOT NULL, result TEXT,
          PRIMARY KEY(turn_id, call_id));
        ''')
        if 'source' not in {r[1] for r in self.db.execute('PRAGMA table_info(events)')}:
            self.db.execute("ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'text'")
        with self.db:
            for row in self.db.execute("SELECT id,turn_id FROM sessions WHERE status='thinking'").fetchall():
                self.db.execute("INSERT INTO events(session_id,turn_id,kind,content,created_at) VALUES (?,?,'interruption',?,?)",
                                (row['id'], row['turn_id'], 'Server restarted; unfinished agent turn was stopped. Inspect the scene before continuing.', time.time()))
            self.db.execute("UPDATE sessions SET status='interrupted' WHERE status='thinking'")

    async def query(self, sql, values=(), fetch=False):
        def run():
            with self.db:
                cursor = self.db.execute(sql, values)
                return [dict(row) for row in cursor.fetchall()] if fetch else cursor.lastrowid
        async with self.lock:
            return await asyncio.to_thread(run)

    async def create(self):
        identifier = str(uuid4())
        await self.query("INSERT INTO sessions VALUES (?, 'idle', NULL)", (identifier,))
        return await self.get(identifier)

    async def get(self, session_id):
        rows = await self.query('SELECT * FROM sessions WHERE id=?', (session_id,), True)
        if not rows:
            raise KeyError('Session not found')
        events = await self.query('SELECT id,turn_id,kind,content,tool_name,call_id,created_at,source FROM events WHERE session_id=? ORDER BY id DESC LIMIT 200', (session_id,), True)
        return ChatSession(session_id=session_id, status=rows[0]['status'], turn_id=rows[0]['turn_id'], events=list(reversed(events)))

    async def set_status(self, session_id, status, turn_id):
        await self.query('UPDATE sessions SET status=?,turn_id=? WHERE id=?', (status,turn_id,session_id))

    async def append(self, session_id, turn_id, kind, content, tool_name=None, call_id=None, source='text'):
        created_at = time.time()
        event_id = await self.query('INSERT INTO events(session_id,turn_id,kind,content,tool_name,call_id,created_at,source) VALUES (?,?,?,?,?,?,?,?)',
                         (session_id,turn_id,kind,content,tool_name,call_id,created_at,source))
        event = dict(id=event_id,session_id=session_id,turn_id=turn_id,kind=kind,content=content,
                     tool_name=tool_name,call_id=call_id,created_at=created_at,source=source)
        for listener in tuple(self.listeners):
            try:
                listener(event)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("Conversation listener failed: %s", type(exc).__name__)
        return event_id

    async def history(self, session_id):
        rows = await self.query("SELECT kind,content FROM events WHERE session_id=? AND kind IN ('user','assistant','interruption') ORDER BY id DESC LIMIT 24", (session_id,), True)
        return [{'role':'assistant' if r['kind']=='assistant' else 'user',
                 'content':r['content'][:8000 if index==len(rows)-1 else 4000] if r['kind']!='interruption' else '[Execution interruption] '+r['content']}
                for index,r in enumerate(reversed(rows))]

    async def prior_request(self, session_id, request_id):
        rows = await self.query('SELECT text,turn_id FROM requests WHERE session_id=? AND request_id=?', (session_id,request_id), True)
        return rows[0] if rows else None

    async def save_request(self, session_id, request_id, text, turn_id):
        await self.query('INSERT INTO requests VALUES (?,?,?,?)', (session_id,request_id,text,turn_id))

    async def receipt(self, turn_id, call_id):
        rows = await self.query('SELECT signature,result FROM receipts WHERE turn_id=? AND call_id=?', (turn_id,call_id), True)
        return rows[0] if rows else None

    async def reserve(self, turn_id, call_id, signature):
        await self.query('INSERT INTO receipts VALUES (?,?,?,NULL)', (turn_id,call_id,signature))

    async def finish(self, turn_id, call_id, result):
        await self.query('UPDATE receipts SET result=? WHERE turn_id=? AND call_id=?', (json.dumps(result),turn_id,call_id))

    async def close(self):
        async with self.lock:
            await asyncio.to_thread(self.db.close)
