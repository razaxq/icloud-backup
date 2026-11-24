"""Deduplicated item outcomes for the current backup, including resumed phases."""


class ItemProgress:
    def __init__(self, db, run_id):
        self.db = db
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS run(id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, ok INTEGER NOT NULL)")
            if db.execute("SELECT id FROM run").fetchone() != (run_id,):
                db.execute("DELETE FROM items")
                db.execute("DELETE FROM run")
                db.execute("INSERT INTO run VALUES (?)", (run_id,))
        self.checked, self.verified = db.execute("SELECT count(*), coalesce(sum(ok), 0) FROM items").fetchone()

    def record(self, key, ok):
        previous = self.db.execute("SELECT ok FROM items WHERE id=?", (key,)).fetchone()
        if previous and previous[0] == ok:
            return
        self.db.execute("INSERT OR REPLACE INTO items VALUES (?,?)", (key, int(ok)))
        self.checked += previous is None
        self.verified += int(ok) - (previous[0] if previous else 0)
