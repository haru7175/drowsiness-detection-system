import threading
import time
from collections import deque
from core.config import DEFAULT_LAT, DEFAULT_LNG

class MovingAverage:
    def __init__(self, size=10):
        self.data = deque(maxlen=size)
    def update(self, val):
        self.data.append(val)
        return sum(self.data) / len(self.data)

class SystemState:
    def __init__(self):
        self.ear = 0.0
        self.mar = 0.0                      # Mouth Aspect Ratio（口の開度・あくび）
        self.pitch = 0.0
        self.roll = 0.0
        self.score = 0.0
        self.drowsiness_level = "Normal"
        self.baseline_ear = 0.0
        self.baseline_pitch = 0.0           # 平常時の頭部Pitch基準値
        self.baseline_roll = 0.0            # 平常時の頭部Roll基準値
        self.consecutive_closed_frames = 0  # 連続閉眼フレーム数
        self.consecutive_yawn_frames = 0    # 連続あくびフレーム数
        self.yawn_count = 0                 # 累計あくび検知回数
        
        # キャリブレーション状態
        self.is_calibrated = False         # キャリブレーション完了フラグ
        self.calib_progress = 0            # キャリブレーション進行度（0-100）
        
        # カメラ・顔追跡状態
        self.camera_connected = False      # カメラ接続状態
        self.last_frame_time = 0.0         # 最終フレーム取得タイムスタンプ
        self.face_detected = True          # 現在フレームで顔が検出されたか
        self.face_lost_start_time = 0.0    # 顔未検出開始タイムスタンプ（わき見・脱力判定用）
        self.is_distracted = False         # わき見・顔見失いフラグ
        
        # 警戒状態・対話後覚醒確認（POST_ALERT: 不感帯防止）
        self.in_post_alert = False         # 対話後の注意警戒中フラグ
        self.post_alert_start_time = 0.0   # 警戒開始時刻
        
        # 位置情報
        self.lat = DEFAULT_LAT
        self.lng = DEFAULT_LNG
        
        # チャット履歴（上限50件でメモリ肥大化を完全防止）
        self._chat_history = deque(maxlen=50)
        self.suggested_spot = "取得中..."
        
        self.navigation = {
            "active": False,
            "spot_name": "",
            "category": "",
            "category_name": "",
            "lat": 0.0,
            "lng": 0.0,
            "distance_text": "",
            "eta_text": "",
            "desc": "",
            "route": [],
            "gmaps_url": ""
        }
        
        self.is_talking = False
        self.lock = threading.Lock()
        self.last_alert_time = 0.0         # 警告音の重複鳴動防止用タイムスタンプ

    @property
    def chat_history(self):
        return list(self._chat_history)

    def add_chat(self, speaker_and_text):
        """履歴に上限付きで追加"""
        self._chat_history.append(speaker_and_text)

# グローバル状態インスタンス
state = SystemState()
