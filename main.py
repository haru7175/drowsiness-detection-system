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
import queue
import winsound

# --- 1. 環境変数とAPIの設定 ---
load_dotenv()

# --- 2. Flask Webサーバーの設定 ---
app = Flask(__name__)

# --- 3. システム状態のグローバル管理 ---
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
        # テスト用の初期位置（笠松駅周辺）
        self.lat = 35.3725
        self.lng = 136.7628
        self.chat_history = []
        self.suggested_spot = "取得中..."
        self.is_talking = False
        self.lock = threading.Lock()
        self.last_alert_time = 0.0  # 警告音の重複鳴動防止用タイムスタンプ

state = SystemState()

# --- TTS（音声合成）の常駐ワーカースレッド（COM競合・フリーズ防止） ---
tts_queue = queue.Queue()

def tts_worker():
    """専用スレッドでpyttsx3を常駐管理し、Windows COMの競合・クラッシュを防ぐ"""
    pythoncom.CoInitialize()
    try:
        engine = pyttsx3.init()
        engine.setProperty('rate', 140)
        while True:
            text = tts_queue.get()
            if text is None:
                break
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception as ex:
                print(f"TTS Speech Error: {ex}")
            finally:
                tts_queue.task_done()
    finally:
        pythoncom.CoUninitialize()

# アプリ起動時にTTS専用スレッドを常駐起動
threading.Thread(target=tts_worker, daemon=True).start()

def speak_text(text, wait=True):
    """安全に音声をキューに送る。wait=Trueの場合は発話完了まで待機（マイクの誤拾い防止）"""
    tts_queue.put(text)
    if wait:
        tts_queue.join()

# --- 即時緊急警告ブザー（遅延ゼロで運転手を覚醒） ---
def play_emergency_alert():
    """居眠り検知（DANGER）の瞬間に即座に高周波ブザーを鳴らす"""
    try:
        print("🚨 [EMERGENCY ALERT] 即時警告ブザー発報！")
        for _ in range(2):
            winsound.Beep(2200, 250)  # 2200Hzの鋭い警告音を250ms鳴らす
            time.sleep(0.05)
    except Exception as ex:
        print(f"Emergency Buzzer Error: {ex}")

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
              (t, round(state.score, 1), round(state.ear, 3), round(state.pitch - state.baseline_pitch, 1), round(state.roll - state.baseline_roll, 1), state.lat, state.lng, action))
    conn.commit()
    conn.close()

# --- 5. マルチモーダル対話エージェント（精度改善版） ---
def sanitize_ai_response(text):
    """LLMの長文やハルシネーション、前置きを切り落とし、運転に適した1文（40文字以内）に整形"""
    if not text:
        return "安全な場所で休憩してくださいね。"
    
    # 前置きや記号の除去
    text = text.replace('"', '').replace('「', '').replace('」', '').replace('\n', ' ').strip()
    for prefix in ["AI:", "アシスタント:", "承知しました、", "ドライバーさん、", "ご提案します。"]:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()

    # 最初の1文（「。」まで）を抽出して長文を防止
    if "。" in text:
        first_sentence = text.split("。")[0] + "。"
        # 1文が長すぎる場合（50文字超）はカット
        if len(first_sentence) <= 50:
            text = first_sentence
        else:
            text = first_sentence[:45] + "。"
    elif len(text) > 45:
        text = text[:42] + "..."

    return text

def ai_agent_task():
    """ドライバーの生体状態・現在地と連動した安全第一の対話エージェント"""
    if state.is_talking: return
    if not state.lock.acquire(blocking=False): return
    
    state.is_talking = True
    try:
        # 1. センシング情報に応じた初回の声かけ（マルチモーダル適応）
        if state.consecutive_closed_frames >= 20:
            initial_msg = "意識はハッキリしていますか？すぐに車を停めて休憩してください。"
        elif state.yawn_count >= 2 or state.consecutive_yawn_frames > 0:
            initial_msg = "あくびが続いていますね。近くのコンビニで一息つきませんか？"
        elif state.pitch - state.baseline_pitch > 9.0:
            initial_msg = "頭が下がってお疲れのようです。少し休憩しませんか？"
        else:
            initial_msg = "お疲れのようです。近くで寄りたい場所はありますか？"

        state.chat_history.append(f"[AI]: {initial_msg}")
        speak_text(initial_msg)

        r = sr.Recognizer()
        r.energy_threshold = 300
        r.dynamic_energy_threshold = True

        for turn in range(2): # 運転負荷軽減のため最大2往復までに制限
            driver_text = ""
            for retry in range(2):
                try:
                    with sr.Microphone() as source:
                        print(f"DEBUG: 音声聞き取り中... (試行 {retry+1}回目)")
                        r.adjust_for_ambient_noise(source, duration=0.3)
                        audio = r.listen(source, timeout=3.5, phrase_time_limit=4)
                    
                    driver_text = r.recognize_google(audio, language='ja-JP').strip()
                    print(f"DEBUG: 認識成功 -> {driver_text}")
                    if driver_text:
                        break
                except Exception as mic_err:
                    print(f"Voice Recognition Retry {retry+1}: {mic_err}")
                    time.sleep(0.3)

            # 【改善1】音声認識失敗時に勝手に「コンビニ」等と捏造しない
            if not driver_text:
                if turn == 0:
                    clarify_msg = "うまく聞き取れませんでした。もう一度教えていただけますか？"
                    state.chat_history.append(f"[AI]: {clarify_msg}")
                    speak_text(clarify_msg)
                    continue  # もう1度だけ聞き直す
                else:
                    final_msg = "安全な場所で無理せず休んでくださいね。"
                    state.chat_history.append(f"[AI]: {final_msg}")
                    speak_text(final_msg)
                    break

            state.chat_history.append(f"[あなた]: {driver_text}")

            # 【改善2】「大丈夫」「起きた」「眠くない」などの即時終了判定
            if any(word in driver_text for word in ["大丈夫", "起きた", "起きてる", "眠くない", "平気", "いらない", "結構", "やめて"]):
                final_msg = "承知しました。目を覚まして安全運転を続けてくださいね。"
                state.chat_history.append(f"[AI]: {final_msg}")
                speak_text(final_msg)
                state.suggested_spot = "安全運転継続中"
                break

            # ユーザーが「決定」「そこにする」などと言った場合の終了
            if any(word in driver_text for word in ["決定", "そこ", "これで", "いいよ", "お願い", "おねがい"]):
                final_msg = "承知しました。気をつけて向かってくださいね。"
                state.chat_history.append(f"[AI]: {final_msg}")
                speak_text(final_msg)
                break

            # スポット自動反映（WebUI・地図連携）
            if any(k in driver_text for k in ["コンビニ", "セブン", "ローソン", "ファミマ"]):
                state.suggested_spot = "最寄りのコンビニ（ローソン・セブン）"
                fallback_reply = "近くのコンビニで休憩しましょう。"
            elif any(k in driver_text for k in ["コーヒー", "カフェ", "飲み物", "お茶"]):
                state.suggested_spot = "最寄りのカフェ・コンビニ"
                fallback_reply = "近くのコンビニで温かい飲み物を買いましょう。"
            elif any(k in driver_text for k in ["パーキング", "PA", "SA", "道の駅", "仮眠", "休む"]):
                state.suggested_spot = "最寄りのPA・道の駅"
                fallback_reply = "この先のパーキングエリアで仮眠を取りましょう。"
            elif any(k in driver_text for k in ["うどん", "ラーメン", "ご飯", "ランチ"]):
                state.suggested_spot = "周辺の飲食店"
                fallback_reply = "近くの飲食店に立ち寄って食事休憩にしましょう。"
            else:
                state.suggested_spot = "最寄りの休憩スポット"
                fallback_reply = "安全な場所に車を停めて一息つきましょう。"

            # 【改善3・4】ハルシネーション防止・架空店名禁止・超短文（1文・40文字以内）プロンプト
            prompt = (
                f"あなたは車の安全運転アシスタントです。\n"
                f"ドライバーの発言: 「{driver_text}」\n\n"
                f"【厳格な制約ルール】\n"
                f"1. 前置きは一切付けず、ドライバーへのセリフのみを【1文（40文字以内）】で返答してください。\n"
                f"2. 架空の店舗名や変な名前（例: ○○マート、△△うどん等）は絶対に創作しないでください。\n"
                f"3. 実在の大手コンビニ（セブン-イレブン、ローソン、ファミリーマート）または「近くのコンビニ」「沿道のパーキングエリア」という一般的な名称のみを使ってください。\n"
                f"4. 運転の邪魔にならない極めて短い自然な日本語で答えてください。"
            )
            
            try:
                print(f"DEBUG: Querying Ollama (Low Temperature) -> {driver_text}")
                response = ollama.chat(
                    model='llama3',
                    messages=[{'role': 'user', 'content': prompt}],
                    options={'temperature': 0.1} # ハルシネーション抑止のため決定論的に設定
                )
                ai_text = response['message']['content'].strip()
                ai_text = sanitize_ai_response(ai_text)
            except Exception as llm_err:
                print(f"Ollama Call Notice: {llm_err}")
                ai_text = fallback_reply

            print(f"DEBUG: Received AI -> {ai_text}")
            state.chat_history.append(f"[AI]: {ai_text}")
            speak_text(ai_text)
            time.sleep(0.5)

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
        state.consecutive_closed_frames = 0
        state.is_talking = False
        state.lock.release()
# --- 6. 映像解析エンジン（MediaPipe版：高精度居眠り・あくび・姿勢推定） ---
def generate_frames():
    mp_face_mesh = mp.solutions.face_mesh
    face_mesh = mp_face_mesh.FaceMesh(max_num_faces=1, refine_landmarks=True, min_detection_confidence=0.5, min_tracking_confidence=0.5)
    
    cap = cv2.VideoCapture(0)
    pitch_ma = MovingAverage(size=10)
    roll_ma = MovingAverage(size=10)
    ear_history = deque(maxlen=300)
    pitch_history = deque(maxlen=100)
    roll_history = deque(maxlen=100)
    
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
            
            # 1. EAR (Eye Aspect Ratio: 眼開度比) 算出
            left_ear = (get_dist(160, 144) + get_dist(158, 153)) / (2.0 * get_dist(33, 133))
            right_ear = (get_dist(385, 380) + get_dist(387, 373)) / (2.0 * get_dist(362, 263))
            current_ear = (left_ear + right_ear) / 2.0

            # 2. MAR (Mouth Aspect Ratio: 口開度比) 算出（あくび検知）
            mouth_height = get_dist(13, 14)
            mouth_width = get_dist(78, 308)
            current_mar = mouth_height / (mouth_width + 1e-6)

            # 3. 頭部姿勢 (Pitch / Roll) 算出
            nose = landmarks[1]
            left_cheek = landmarks[234]
            right_cheek = landmarks[454]
            raw_pitch = (nose.y - (left_cheek.y + right_cheek.y) / 2) * 100
            raw_roll = (left_cheek.y - right_cheek.y) * 100

            state.ear = current_ear
            state.mar = current_mar
            state.pitch = pitch_ma.update(raw_pitch)
            state.roll = roll_ma.update(raw_roll)
            ear_history.append(current_ear)

            # 4. キャリブレーション（個人差・カメラ角度の自動学習）
            if not is_calibrated:
                calibration_frames += 1
                pitch_history.append(raw_pitch)
                roll_history.append(raw_roll)
                
                # 画面上にキャリブレーション進行状況を描画
                cv2.rectangle(frame, (15, 15), (310, 65), (0, 0, 0), -1)
                cv2.putText(frame, f"Calibrating: {calibration_frames}/100", (25, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
                
                if calibration_frames >= 100:
                    sorted_ear = np.sort(list(ear_history))
                    state.baseline_ear = float(np.mean(sorted_ear[-40:]))  # 通常開眼時の中央〜上位値
                    state.baseline_pitch = float(np.median(list(pitch_history)))
                    state.baseline_roll = float(np.median(list(roll_history)))
                    is_calibrated = True
                    print(f"Calibration Complete! Base EAR: {state.baseline_ear:.3f}, Base Pitch: {state.baseline_pitch:.1f}")
            else:
                # 走行中の環境光・カメラ露出適応（直近開眼度上位から緩やかに再計算）
                if len(ear_history) == 300:
                    sorted_ear = np.sort(list(ear_history))
                    new_base = float(np.mean(sorted_ear[-40:]))
                    state.baseline_ear = 0.95 * state.baseline_ear + 0.05 * new_base

                # --- 居眠り・疲労度・姿勢の常時リアルタイム判定 ---
                ear_ratio = state.ear / (state.baseline_ear + 0.001)
                
                # --- A. 連続閉眼トラッキング（自然な瞬きと居眠りを峻別） ---
                is_closed = (ear_ratio < 0.65)
                if is_closed:
                    state.consecutive_closed_frames += 1
                    # 自然な瞬き（1〜8フレーム ≒ 約0.25秒）はスコア加算なし
                    if state.consecutive_closed_frames > 8:
                        state.score += 1.0  # 緩慢な瞬き
                    if state.consecutive_closed_frames > 20:
                        state.score += 2.5  # 明らかな閉眼・居眠り
                    # 35フレーム以上（約1.2秒以上）目を閉じ続けている場合は即座にDANGER
                    if state.consecutive_closed_frames >= 35:
                        state.score = max(state.score, 45.0)
                else:
                    state.consecutive_closed_frames = 0
                    # 目を開けているときは覚醒度に応じて速やかにスコアを減衰（約1秒でNORMALへ復帰）
                    if ear_ratio >= 0.80:
                        state.score = max(0.0, state.score - 1.5)  # しっかり開眼時は高速回復
                    else:
                        state.score = max(0.0, state.score - 0.6)

                # --- B. あくび検知（MAR: 眠気の予兆をキャッチ） ---
                if current_mar > 0.52:
                    state.consecutive_yawn_frames += 1
                    if state.consecutive_yawn_frames == 18:  # 約0.6秒あくびが持続
                        state.yawn_count += 1
                        state.score += 8.0  # あくびによるスコア加算
                        print(f"🥱 [YAWN DETECTED] あくび検知！ 累計: {state.yawn_count}回")
                else:
                    state.consecutive_yawn_frames = max(0, state.consecutive_yawn_frames - 1)

                # --- C. 頭部姿勢の平常時からのズレ判定（うなずき・脱力） ---
                delta_pitch = state.pitch - state.baseline_pitch
                delta_roll = state.roll - state.baseline_roll
                if delta_pitch > 9.0 or abs(delta_roll) > 10.0:
                    state.score += 1.2

                # --- D. 2段階ハイブリッド警告 & 常時ステータス更新 ---
                now = time.time()
                if state.score > 40:
                    state.drowsiness_level = "DANGER (居眠り危険)"
                    # クールダウン（3秒間隔）
                    if now - state.last_alert_time > 3.0:
                        state.last_alert_time = now
                        save_detailed_log("Emergency Alert & Buzzer Triggered")
                        # ① 遅延ゼロ（0.05秒）で即時警告ブザー発報（運転手を即座に覚醒）
                        threading.Thread(target=play_emergency_alert, daemon=True).start()
                        # ② 対話中でなければバックグラウンドで対話エージェントも起動
                        if not state.is_talking:
                            threading.Thread(target=ai_agent_task, daemon=True).start()
                elif state.score > 20 or state.consecutive_closed_frames > 15:
                    state.drowsiness_level = "WARNING (眠気・疲労予兆)"
                else:
                    state.drowsiness_level = "NORMAL (安全)"

                # --- 画面上へのリアルタイムHUD描画 ---
                delta_p = state.pitch - state.baseline_pitch
                ear_pct = int((state.ear / (state.baseline_ear + 1e-4)) * 100)
                hud_color = (0, 255, 0) if "NORMAL" in state.drowsiness_level else ((0, 200, 255) if "WARNING" in state.drowsiness_level else (0, 0, 255))
                
                cv2.putText(frame, f"EAR: {state.ear:.2f} ({ear_pct}%)", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
                cv2.putText(frame, f"MAR: {state.mar:.2f}", (15, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
                cv2.putText(frame, f"Pitch: {delta_p:+.1f}", (15, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
                cv2.putText(frame, f"{state.drowsiness_level}", (15, 95), cv2.FONT_HERSHEY_SIMPLEX, 0.65, hud_color, 2)

            # 目と口の特徴点ガイドを描画
            for p in [33, 133, 362, 263, 13, 14, 78, 308]:
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
    delta_pitch = round(state.pitch - state.baseline_pitch, 1)
    return jsonify({
        "score": round(state.score, 1),
        "ear": round(state.ear, 2),
        "mar": round(state.mar, 2),
        "pitch": delta_pitch,
        "yawn_count": state.yawn_count,
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