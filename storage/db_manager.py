import sqlite3
import queue
import threading
from datetime import datetime
from core.config import DATABASE_NAME
from core.state import state

log_queue = queue.Queue()

def log_worker():
    """SQLiteへの書き込みを専用ワーカースレッドで直列処理し、カメラ処理のFPS低下をゼロにする"""
    conn = sqlite3.connect(DATABASE_NAME, check_same_thread=False)
    while True:
        log_entry = log_queue.get()
        if log_entry is None:
            break
        t, score, ear, pitch, roll, lat, lng, action = log_entry
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO detailed_logs (timestamp, score, ear, pitch, roll, lat, lng, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (t, score, ear, pitch, roll, lat, lng, action)
            )
            conn.commit()
        except Exception as ex:
            print(f"Async DB Log Error: {ex}")
        finally:
            log_queue.task_done()

# 常駐ワーカースレッドの起動
threading.Thread(target=log_worker, daemon=True).start()

def init_db():
    """データベーステーブルの初期化"""
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
        name TEXT PRIMARY KEY,
        baseline_ear REAL,
        avg_blink_rate REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS detailed_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        score REAL,
        ear REAL,
        pitch REAL,
        roll REAL,
        lat REAL,
        lng REAL,
        action TEXT
    )''')
    conn.commit()
    conn.close()

def save_detailed_log(action):
    """カメラメインスレッドをブロックしない非同期キュー投入"""
    t = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_queue.put((
        t,
        round(state.score, 1),
        round(state.ear, 3),
        round(state.pitch - state.baseline_pitch, 1),
        round(state.roll - state.baseline_roll, 1),
        state.lat,
        state.lng,
        action
    ))

def get_recent_logs(limit=20):
    """直近の走行ログを取得（可視化・分析用）"""
    try:
        conn = sqlite3.connect(DATABASE_NAME)
        c = conn.cursor()
        c.execute("SELECT timestamp, score, ear, pitch, roll, action FROM detailed_logs ORDER BY id DESC LIMIT ?", (limit,))
        rows = c.fetchall()
        conn.close()
        return [
            {
                "timestamp": r[0],
                "score": r[1],
                "ear": r[2],
                "pitch": r[3],
                "roll": r[4],
                "action": r[5]
            } for r in rows
        ]
    except Exception as ex:
        print(f"Get Logs Error: {ex}")
        return []
