import os
import sys
import time
import threading
import webbrowser
import numpy as np
from flask import Flask, render_template, Response, request, jsonify

# 各モジュールのインポート
from core.config import DEFAULT_LAT, DEFAULT_LNG
from core.state import state, SystemState, MovingAverage
from storage.db_manager import init_db, save_detailed_log, get_recent_logs
from voice.tts_engine import speak_text, play_emergency_alert
from voice.dialog_agent import trigger_dialog, ai_agent_task, sanitize_ai_response
from navigation.spot_navigator import (
    find_best_spot,
    resolve_dynamic_spot,
    start_navigation,
    cancel_navigation,
    is_pure_affirmation,
    extract_destination_query,
    calculate_distance_km
)
from sensing.face_tracker import generate_frames, get_camera_capture

app = Flask(__name__)

# --- 7. Webダッシュボード用ルート (API) ---
@app.route('/')
def index():
    gmaps_api_key = os.getenv('GMAPS_API_KEY', '').strip("'\"")
    return render_template('index.html', gmaps_api_key=gmaps_api_key)

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/status')
def api_status():
    delta_pitch = round(state.pitch - state.baseline_pitch, 1)
    is_cam_alive = state.camera_connected and (time.time() - state.last_frame_time < 2.0)
    clamped_score = round(float(np.clip(state.score, 0.0, 100.0)), 1)
    return jsonify({
        "score": clamped_score,
        "ear": round(state.ear, 2),
        "mar": round(state.mar, 2),
        "pitch": delta_pitch,
        "yawn_count": state.yawn_count,
        "drowsiness_level": state.drowsiness_level,
        "suggested_spot": state.suggested_spot,
        "chat_history": state.chat_history[:8],
        "camera_connected": is_cam_alive,
        "is_distracted": state.is_distracted,
        "in_post_alert": state.in_post_alert,
        "navigation": state.navigation
    })

@app.route('/api/gps', methods=['POST'])
def api_gps():
    data = request.json or {}
    state.lat = data.get('lat', state.lat)
    state.lng = data.get('lng', state.lng)
    return jsonify({"status": "success"})

@app.route('/api/navigate', methods=['POST'])
def api_navigate():
    """フロントエンドのクイックボタン等から指定スポット/キーワードでナビゲーションを開始"""
    data = request.json or {}
    query = data.get('query', 'コンビニ')
    spot = find_best_spot(query, state.lat, state.lng)
    start_navigation(spot, speak_fn=speak_text, speak=data.get('speak', True))
    return jsonify({"status": "success", "navigation": state.navigation})

@app.route('/api/cancel_navigate', methods=['POST'])
def api_cancel_navigate():
    """ナビゲーション終了・クリア"""
    cancel_navigation()
    return jsonify({"status": "success"})

@app.route('/api/driver_say', methods=['POST'])
def api_driver_say():
    """WebUIからテキスト入力でドライバーの発言を処理・案内を開始するエンドポイント"""
    data = request.json or {}
    text = data.get('text', '').strip()
    if not text:
        return jsonify({"status": "error", "message": "テキストが空です"}), 400
    
    def process_say(driver_text):
        state.add_chat(f"[あなた]: {driver_text}")
        if any(word in driver_text for word in ["大丈夫", "起きた", "眠くない", "平気", "キャンセル"]):
            reply = "承知しました。目を覚まして安全運転を続けてくださいね。"
            cancel_navigation()
        elif is_pure_affirmation(driver_text):
            spot = find_best_spot("コンビニ", state.lat, state.lng)
            start_navigation(spot, speak_fn=speak_text, speak=False)
            reply = f"かしこまりました！{spot['name']}へ案内を開始しますね。気をつけて向かってください。"
        else:
            spot = find_best_spot(driver_text, state.lat, state.lng)
            start_navigation(spot, speak_fn=speak_text, speak=False)
            reply = f"かしこまりました！{spot['name']}へ案内を開始しますね。気をつけて向かってください。"
        state.add_chat(f"[AI]: {reply}")
        speak_text(reply)

    threading.Thread(target=process_say, args=(text,), daemon=True).start()
    return jsonify({"status": "success"})

@app.route('/api/trigger_alert', methods=['POST'])
def api_trigger_alert():
    """テスト用に即時警告＆提案対話を起動"""
    threading.Thread(target=play_emergency_alert, daemon=True).start()
    trigger_dialog()
    return jsonify({"status": "success"})

@app.route('/api/logs', methods=['GET'])
def api_logs():
    """直近の走行生体ログを返却"""
    limit = request.args.get('limit', 20, type=int)
    logs = get_recent_logs(limit=limit)
    return jsonify({"status": "success", "logs": logs})

# --- メイン実行 ---
if __name__ == '__main__':
    init_db()
    print("===================================================")
    print("[START] マルチモーダル対話型 AIダッシュボード v2.0 起動準備完了！")
    print("===================================================")
    
    def open_browser():
        time.sleep(2)
        webbrowser.open('http://127.0.0.1:5000')

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='127.0.0.1', port=5000, threaded=True, debug=False)
