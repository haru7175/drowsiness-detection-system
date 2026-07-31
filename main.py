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

        # 初回の話しかけ
        initial_msg = "お疲れのようです。休憩しませんか？近くで食べたいものや、寄りたい場所はありますか？"
        state.chat_history.append(f"[AI]: {initial_msg}")
        speak_text(initial_msg)

        # チャットを複数回繰り返すループ（お店が決まるまで）
        r = sr.Recognizer()
        r.energy_threshold = 300
        r.dynamic_energy_threshold = True

        for turn in range(3): # 最大3往復まで対話を継続
            driver_text = ""
            
            # 音声認識（聞き取りに失敗した場合に最大3回までリトライする仕組み）
            for retry in range(3):
                try:
                    with sr.Microphone() as source:
                        print(f"DEBUG: 音声聞き取り中... (試行 {retry+1}回目)")
                        r.adjust_for_ambient_noise(source, duration=0.5)
                        audio = r.listen(source, timeout=6, phrase_time_limit=5)
                    
                    driver_text = r.recognize_google(audio, language='ja-JP')
                    print(f"DEBUG: 認識成功 -> {driver_text}")
                    if driver_text:
                        break # うまく聞き取れたらリトライループを抜ける
                except Exception as mic_err:
                    print(f"Voice Recognition Retry {retry+1} Error: {mic_err}")
                    if retry == 2:
                        # 3回とも失敗した場合のフォールバック
                        driver_text = "ラーメン"
                    else:
                        time.sleep(0.5)

            state.chat_history.append(f"[あなた]: {driver_text}")

            # ユーザーが「決定」「そこにする」などと言った場合の終了判定
            if any(word in driver_text for word in ["決定", "そこ", "これで", "いいよ", "お愿い", "お願い"]):
                final_msg = "承知しました。安全に気をつけて向かってくださいね。"
                state.chat_history.append(f"[AI]: {final_msg}")
                speak_text(final_msg)
                state.suggested_spot = driver_text
                break

            # GPS情報の反映
            location_desc = "現在地周辺の飲食店やコンビニ"
            if state.lat and state.lng:
                location_desc = f"現在地（緯度:{state.lat:.4f}, 経度:{state.lng:.4f}）の周辺にある具体的な飲食店やコンビニ"

            
         # GPS情報の反映（岐阜県笠松町のテスト環境を明確にする）
            current_place_name = "岐阜県羽島郡笠松町（笠松駅周辺）"
            location_desc = f"現在地は「{current_place_name}」です（緯度:{state.lat:.4f}, 経度:{state.lng:.4f}）。この周辺に実際にある一般的な飲食店、うどん店、コンビニ（ローソンやセブンイレブンなど）を考慮してください。"

            # これまでの会話履歴をまとめる
            history_text = "\n".join(state.chat_history[-6:])

            # Ollamaへのプロンプト（勝手な決めつけや架空の店を防ぎ、現在地を厳守させる）
            prompt = (
                f"あなたは自動車の安全運転をサポートするスマートアシスタントです。\n"
                f"これまでの会話の文脈:\n{history_text}\n\n"
                f"最新のドライバーの発言: 「{driver_text}」\n"
                f"周辺状況: {location_desc}。\n\n"
                f"【絶対厳守のルール】\n"
                f"1. すべて完全に自然な日本語のみで出力してください（英語禁止）。\n"
                f"2. 前置きや思考プロセス（「〜を提案します」等）は一切出力せず、ドライバーへのセリフのみを1〜2文で出力してください。\n"
                f"3. **存在しない架空の店名や変な名前（例: 新宿拉麺王、やきいもうどん屋など）を絶対に創作して答えないでください。**\n"
                f"4. お店や場所を提案するときは、「近くのうどん店」「コンビニ（ローソンやセブンイレブンなど）」「沿道のパーキングエリア」といった現実的で一般的な名称のみを使ってください。\n"
                f"5. ドライバーが「うどんが食べたい」と言えば、「周辺のうどん店」を、「ローソンが良い」と言えば「ローソン」に沿って、まっすぐ優しく返答してください。"
            )
            
            print(f"DEBUG: Sending to Ollama -> {driver_text}")
            response = ollama.chat(model='llama3', messages=[{'role': 'user', 'content': prompt}])
            ai_text = response['message']['content'].strip()
            print(f"DEBUG: Received from Ollama -> {ai_text}")
            
            
            state.chat_history.append(f"[AI]: {ai_text}")
            speak_text(ai_text)
            
            # 次のループへ進む前に少し待機
            time.sleep(1)

    except Exception as e:
        print(f"CRITICAL AI AGENT ERROR: {e}")
        fallback_msg = "安全な場所で休憩してくださいね。"
        state.chat_history.append(f"[AI]: {fallback_msg}")
        try:
            speak_text(fallback_msg)
        except:
            pass
    finally:
        state.score = 0  # 対話が終わったらスコアをリセット
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