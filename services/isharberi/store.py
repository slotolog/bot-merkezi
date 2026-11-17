"""Yerel veri (SQLite): kullanıcılar, haberler, kayıtlılar."""
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, lang TEXT,
    joined_at INTEGER, ref TEXT, blocked INTEGER DEFAULT 0,
    mode TEXT DEFAULT 'instant',          -- instant / bulletin / off
    bulletin_hour INTEGER DEFAULT 9,
    cats TEXT DEFAULT 'kamu,ozel,iskur,staj,maas',
    keywords TEXT DEFAULT '',
    alert_day TEXT, alerts_today INTEGER DEFAULT 0,
    last_bulletin TEXT, onboarded INTEGER DEFAULT 0, last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY, cat TEXT, title TEXT, source TEXT, link TEXT,
    published INTEGER, fetched INTEGER
);
CREATE INDEX IF NOT EXISTS ix_items_cat ON items(cat, published);
CREATE TABLE IF NOT EXISTS saved (user_id INTEGER, item_id TEXT, at INTEGER, PRIMARY KEY(user_id, item_id));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- kv ----
    def get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set(self, k, v):
        self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        self.db.commit()

    # ---- users ----
    def user(self, uid):
        return self.db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

    def upsert_user(self, uid, username, first_name, lang, ref=None) -> bool:
        """Yeni kullanıcıysa True."""
        now = int(time.time())
        if self.user(uid):
            self.db.execute("UPDATE users SET username=?, first_name=?, blocked=0, last_seen=? WHERE user_id=?",
                            (username, first_name, now, uid))
            self.db.commit()
            return False
        self.db.execute("INSERT INTO users(user_id,username,first_name,lang,joined_at,ref,last_seen) VALUES(?,?,?,?,?,?,?)",
                        (uid, username, first_name, lang, now, ref, now))
        self.db.commit()
        return True

    def update_user(self, uid, **kw):
        if not kw:
            return
        cols = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE users SET {cols} WHERE user_id=?", (*kw.values(), uid))
        self.db.commit()

    def active_users(self):
        return self.db.execute("SELECT * FROM users WHERE blocked=0").fetchall()

    def all_users(self):
        return self.db.execute("SELECT * FROM users").fetchall()

    def import_user(self, uid, username, first_name, lang, blocked, joined_at=None, ref="merkez",
                    mode="bulletin", bulletin_hour=12):
        """Merkez'den gelen abone; varsayılan günlük bülten (ani bildirim yağmuru olmasın)."""
        if self.user(uid):
            return False
        self.db.execute("INSERT INTO users(user_id,username,first_name,lang,joined_at,ref,blocked,onboarded,mode,bulletin_hour) "
                        "VALUES(?,?,?,?,?,?,?,1,?,?)",
                        (uid, username, first_name, lang, joined_at or int(time.time()), ref or "merkez",
                         1 if blocked else 0, mode, bulletin_hour))
        self.db.commit()
        return True

    # ---- items ----
    def add_item(self, it: dict) -> bool:
        cur = self.db.execute("INSERT OR IGNORE INTO items(id,cat,title,source,link,published,fetched) VALUES(?,?,?,?,?,?,?)",
                              (it["id"], it["cat"], it["title"], it["source"], it["link"], it["published"], it["fetched"]))
        self.db.commit()
        return cur.rowcount > 0

    def item(self, iid):
        return self.db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()

    def latest(self, cats: list[str] | None, limit=5, offset=0, since=None):
        q, args = "SELECT * FROM items WHERE 1=1", []
        if cats:
            q += f" AND cat IN ({','.join('?' * len(cats))})"
            args += cats
        if since:
            q += " AND published >= ?"
            args.append(since)
        q += " ORDER BY published DESC LIMIT ? OFFSET ?"
        return self.db.execute(q, (*args, limit, offset)).fetchall()

    def search(self, words: list[str], limit=8):
        q = "SELECT * FROM items WHERE " + " AND ".join("lower(title) LIKE ?" for _ in words)
        q += " ORDER BY published DESC LIMIT ?"
        return self.db.execute(q, (*[f"%{w}%" for w in words], limit)).fetchall()

    def prune(self, days=21):
        cut = int(time.time()) - days * 86400
        self.db.execute("DELETE FROM items WHERE published < ? AND id NOT IN (SELECT item_id FROM saved)", (cut,))
        self.db.commit()

    # ---- saved ----
    def save(self, uid, iid) -> bool:
        cur = self.db.execute("INSERT OR IGNORE INTO saved(user_id,item_id,at) VALUES(?,?,?)", (uid, iid, int(time.time())))
        self.db.commit()
        return cur.rowcount > 0

    def unsave(self, uid, iid):
        self.db.execute("DELETE FROM saved WHERE user_id=? AND item_id=?", (uid, iid))
        self.db.commit()

    def saved(self, uid, limit=10):
        return self.db.execute("SELECT i.* FROM saved s JOIN items i ON i.id=s.item_id WHERE s.user_id=? "
                               "ORDER BY s.at DESC LIMIT ?", (uid, limit)).fetchall()
