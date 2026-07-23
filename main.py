import cv2
import mediapipe as mp
import sqlite3
import numpy as np
import threading
import pyttsx3
import speech_recognition as sr
import os
import time
from datetime import datetime
from collections import deque
from dotenv import load_dotenv
from flask import Flask, render_template, Response, request, jsonify
import pythoncom
import webbrowser
import ollama

# --- 1. 環境変数とAPIの設定 ---
load_dotenv()

# --- 2. Flask Webサーバーの設定 ---
app = Flask(__name__)

# --- 3. システム状態のグローバル管理 ---
class SystemState:
    def __init__(self):
        self.ear = 0.0
        self.pitch = 0.0
        self.roll = 0.0
        self.score = 0.0
        self.drowsiness_level = "Normal"
        self.baseline_ear = 0.0
        # テスト用の初期位置（笠松駅周辺）
        self.lat = 35.3725
        self.lng = 136.7628
        self.chat_history = []
        self.suggested_spot = "取得中..."
        self.is_talking = False
        self.lock = threading.Lock()

state = SystemState()

class MovingAverage:
    def __init__(self, size=10):
        self.data = deque(maxlen=size)
    def update(self, val):
        self.data.append(val)
        return sum(self.data) / len(self.data)

# --- 4. データベースと学習機能 ---
def init_db():
    conn = sqlite3.connect('mobility_advanced.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles (name TEXT PRIMARY KEY, baseline_ear REAL, avg_blink_rate REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS detailed_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, score REAL, ear REAL, pitch REAL, roll REAL, lat REAL, lng REAL, action TEXT)''')
    conn.commit()
    conn.close()

def save_detailed_log(action):
    conn = sqlite3.connect('mobility_advanced.db')
    c = conn.cursor()
    t = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute("INSERT INTO detailed_logs (timestamp, score, ear, pitch, roll, lat, lng, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (t, state.score, state.ear, state.pitch, state.roll, state.lat, state.lng, action))
    conn.commit()
    conn.close()

# --- 5. 対話エージェント ---
def ai_agent_task():
    if state.is_talking: return
    if not state.lock.acquire(blocking=False): return
    
    state.is_talking = True
    engine = None
    try:
        pythoncom.CoInitialize()
        
        def speak_text(text):
            try:
                local_engine = pyttsx3.init()
                local_engine.setProperty('rate', 140)
                local_engine.say(text)
                local_engine.runAndWait()
            except Exception as ex:
                print(f"TTS Speech Error: {ex}")

        # 1. 警告とヒアリング
        msg1 = "お疲れのようです。休憩しませんか？何か食べたいものはありますか？"
        state.chat_history.append(f"[AI]: {msg1}")
        speak_text(msg1)

        # 2. 音声認識
        r = sr.Recognizer()
        r.energy_threshold = 300
        r.dynamic_energy_threshold = True
        driver_text = ""
        
        try:
            with sr.Microphone() as source:
                r.adjust_for_ambient_noise(source, duration=0.8)
                audio = r.listen(source, timeout=8, phrase_time_limit=6)
            driver_text = r.recognize_google(audio, language='ja-JP')
        except Exception as mic_err:
            print(f"Voice Recognition Error: {mic_err}")
            driver_text = "うどん" # フォールバック

        state.chat_history.append(f"[あなた]: {driver_text}")

        # 3. GPS座標に基づいたスポット判定
        location_desc = "現在地周辺の飲食店やロードサイドのうどん店・コンビニ"
        if state.lat and state.lng:
            location_desc = f"現在地（緯度:{state.lat:.4f}, 経度:{state.lng:.4f}）の周辺にある、うどん等の飲食店やコンビニ・パーキング"

        # 4. Ollama連携
        prompt = (
            f"あなたは自動車のスマートアシスタントです。"
            f"ドライバーが「{driver_text}」を食べたいと言っています。"
            f"周辺情報として「{location_desc}」を考慮してください。"
            f"思考のプロセス（「〜を提案します」等の解説）は一切出力せず、"
            f"ドライバーへ語りかける自然な日本語のセリフのみを1〜2文で出力してください。"
            f"最後に安全運転を促す言葉を入れてください。"
        )
        
        print(f"DEBUG: Sending to Ollama -> {driver_text}")
        response = ollama.chat(model='llama3', messages=[{'role': 'user', 'content': prompt}])
        ai_text = response['message']['content'].strip()
        print(f"DEBUG: Received from Ollama -> {ai_text}")
        
        state.chat_history.append(f"[AI]: {ai_text}")
        speak_text(ai_text)

    except Exception as e:
        print(f"CRITICAL AI AGENT ERROR: {e}")
        fallback_msg = "安全な場所で休憩してくださいね。"
        state.chat_history.append(f"[AI]: {fallback_msg}")
        try:
            speak_text(fallback_msg)
        except:
            pass
    finally:
        state.score = 0
        state.is_talking = False
        state.lock.release()
        try:
            pythoncom.CoUninitialize()
        except:
            pass

# --- 6. 映像解析エンジン（MediaPipe版） ---
def generate_frames():
    mp_face_mesh = mp.solutions.face_mesh
    face_mesh = mp_face_mesh.FaceMesh(max_num_faces=1, refine_landmarks=True, min_detection_confidence=0.5, min_tracking_confidence=0.5)
    
    cap = cv2.VideoCapture(0)
    pitch_ma = MovingAverage(size=10)
    roll_ma = MovingAverage(size=10)
    ear_history = deque(maxlen=300)
    
    calibration_frames = 0
    is_calibrated = False

    while True:
        success, frame = cap.read()
        if not success: break
        
        frame = cv2.flip(frame, 1)
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = face_mesh.process(rgb_frame)

        if results.multi_face_landmarks:
            landmarks = results.multi_face_landmarks[0].landmark
            h, w, _ = frame.shape

            def get_dist(p1, p2):
                return np.linalg.norm(np.array([landmarks[p1].x * w, landmarks[p1].y * h]) - np.array([landmarks[p2].x * w, landmarks[p2].y * h]))
            
            left_ear = (get_dist(160, 144) + get_dist(158, 153)) / (2.0 * get_dist(33, 133))
            right_ear = (get_dist(385, 380) + get_dist(387, 373)) / (2.0 * get_dist(362, 263))
            current_ear = (left_ear + right_ear) / 2.0
            
            nose = landmarks[1]
            left_cheek = landmarks[234]
            right_cheek = landmarks[454]
            raw_pitch = (nose.y - (left_cheek.y + right_cheek.y) / 2) * 100
            raw_roll = (left_cheek.y - right_cheek.y) * 100

            state.ear = current_ear
            state.pitch = pitch_ma.update(raw_pitch)
            state.roll = roll_ma.update(raw_roll)
            ear_history.append(current_ear)

            if not is_calibrated:
                calibration_frames += 1
                cv2.putText(frame, f"Calibrating: {calibration_frames}/100", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
                if calibration_frames >= 100:
                    state.baseline_ear = np.mean(list(ear_history)[-50:])
                    is_calibrated = True
            else:
                if len(ear_history) == 300:
                    sorted_ear = np.sort(list(ear_history))
                    state.baseline_ear = np.mean(sorted_ear[-50:])

                if not state.is_talking:
                    ear_ratio = state.ear / (state.baseline_ear + 0.001)
                    
                    # 1. しっかりと目が閉じている瞬間を判定
                    is_eyes_closed = (ear_ratio < 0.65)
                    
                    if is_eyes_closed:
                        state.score += 0.8
                    else:
                        state.score = max(0, state.score - 0.6)

                    # 2. 姿勢の乱れ判定
                    if state.pitch > 8.0 or abs(state.roll) > 8.0:
                        state.score += 1.0

                    # 3. 閾値によるレベル分け（重複を排除して一本化）
                    if state.score > 40:
                        state.drowsiness_level = "DANGER (眠気検知)"
                        save_detailed_log("AI Intervention Triggered")
                        threading.Thread(target=ai_agent_task).start()
                    elif state.score > 20:
                        state.drowsiness_level = "WARNING (やや疲れ)"
                    else:
                        state.drowsiness_level = "NORMAL (安全)"

            for p in [33, 133, 362, 263, 1]:
                cv2.circle(frame, (int(landmarks[p].x * w), int(landmarks[p].y * h)), 2, (0, 255, 0), -1)

        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

# --- 7. Webダッシュボード用ルート (API) ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/status')
def api_status():
    return jsonify({
        "score": state.score,
        "ear": state.ear,
        "pitch": state.pitch,
        "drowsiness_level": state.drowsiness_level,
        "suggested_spot": state.suggested_spot,
        "chat_history": state.chat_history[:5]
    })

@app.route('/api/gps', methods=['POST'])
def api_gps():
    data = request.json
    state.lat = data.get('lat', state.lat)
    state.lng = data.get('lng', state.lng)
    
    if state.lat:
        state.suggested_spot = "最寄りのコンビニ・パーキング"
    return jsonify({"status": "success"})

# --- 8. メイン実行 ---
if __name__ == '__main__':
    init_db()
    print("===================================================")
    print("🚗 マルチモーダル対話型 AIダッシュボード 起動準備完了！")
    print("===================================================")
    
    def open_browser():
        time.sleep(2)
        webbrowser.open('http://127.0.0.1:5000')

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='127.0.0.1', port=5000, threaded=True, debug=False)