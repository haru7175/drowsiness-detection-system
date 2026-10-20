import cv2
import mediapipe as mp
import numpy as np
import time
from collections import deque
import threading
from core.config import draw_text_jp, JP_FONT_HUD, JP_FONT_HUD_SM
from core.state import state, MovingAverage
from voice.tts_engine import play_emergency_alert
from voice.dialog_agent import trigger_dialog
from storage.db_manager import save_detailed_log

def get_camera_capture():
    """Windows環境で高速・安定してカメラをオープン（DirectShow優先 & インデックスフォールバック）"""
    for idx in [0, 1]:
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if cap.isOpened():
            ret, _ = cap.read()
            if ret:
                print(f"[OK] カメラ起動成功 (DirectShow, index={idx})")
                return cap
            cap.release()
        
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            ret, _ = cap.read()
            if ret:
                print(f"[OK] カメラ起動成功 (Default, index={idx})")
                return cap
            cap.release()

    print("[WARN] カメラデバイスが見つかりません (index 0, 1)")
    return None

def generate_frames():
    """MediaPipe Face Meshを用いた高精度生体センシング & リアルタイムMJPEGストリーミング"""
    mp_face_mesh = mp.solutions.face_mesh
    face_mesh = mp_face_mesh.FaceMesh(
        max_num_faces=1,
        refine_landmarks=True,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5
    )
    
    cap = get_camera_capture()
    if cap is None:
        state.camera_connected = False
        blank = np.zeros((480, 640, 3), dtype=np.uint8)
        blank = draw_text_jp(blank, "カメラを検出できませんでした", (150, 220), JP_FONT_HUD, (0, 0, 255))
        blank = draw_text_jp(blank, "USB接続・アクセス権限を確認してください", (120, 260), JP_FONT_HUD_SM, (200, 200, 200))
        ret, buffer = cv2.imencode('.jpg', blank)
        frame_bytes = buffer.tobytes()
        while True:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(1.0)

    pitch_ma = MovingAverage(size=10)
    roll_ma = MovingAverage(size=10)
    ear_history = deque(maxlen=300)
    pitch_history = deque(maxlen=100)
    roll_history = deque(maxlen=100)
    
    valid_calib_samples = 0
    is_calibrated = False

    try:
        while True:
            success, frame = cap.read()
            if not success:
                state.camera_connected = False
                time.sleep(0.1)
                break
            
            state.camera_connected = True
            state.last_frame_time = time.time()
            now = time.time()
            
            frame = cv2.flip(frame, 1)
            h, w, _ = frame.shape
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = face_mesh.process(rgb_frame)

            if results.multi_face_landmarks:
                state.face_detected = True
                state.face_lost_start_time = 0.0
                state.is_distracted = False
                
                landmarks = results.multi_face_landmarks[0].landmark

                def get_dist(p1, p2):
                    return np.linalg.norm(np.array([landmarks[p1].x * w, landmarks[p1].y * h]) - np.array([landmarks[p2].x * w, landmarks[p2].y * h]))
                
                # 1. EAR (Eye Aspect Ratio: 眼開度比) 算出
                left_ear = (get_dist(160, 144) + get_dist(158, 153)) / (2.0 * get_dist(33, 133))
                right_ear = (get_dist(385, 380) + get_dist(387, 373)) / (2.0 * get_dist(362, 263))
                current_ear = (left_ear + right_ear) / 2.0

                # 2. MAR (Mouth Aspect Ratio: 口開度比) 算出
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

                # 4. キャリブレーション（異常値除外バリデーション付き）
                if not is_calibrated:
                    # 閉眼（EAR < 0.18）や極端な傾きは学習から除外
                    is_valid_pose = (current_ear >= 0.18) and (abs(raw_pitch) < 25) and (abs(raw_roll) < 25)
                    if is_valid_pose:
                        valid_calib_samples += 1
                        pitch_history.append(raw_pitch)
                        roll_history.append(raw_roll)
                    
                    cv2.rectangle(frame, (15, 15), (380, 75), (0, 0, 0), -1)
                    cv2.putText(frame, f"Calibrating: {valid_calib_samples}/100", (25, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 255), 2)
                    frame = draw_text_jp(frame, "正面を向いて目を開けてください", (25, 52), JP_FONT_HUD_SM, (200, 255, 200))
                    
                    if valid_calib_samples >= 100:
                        sorted_ear = np.sort(list(ear_history))
                        state.baseline_ear = float(np.mean(sorted_ear[-40:]))
                        state.baseline_pitch = float(np.median(list(pitch_history)))
                        state.baseline_roll = float(np.median(list(roll_history)))
                        is_calibrated = True
                        print(f"Calibration Complete! Base EAR: {state.baseline_ear:.3f}, Base Pitch: {state.baseline_pitch:.1f}")
                else:
                    # 環境光・露出適応
                    if len(ear_history) == 300:
                        sorted_ear = np.sort(list(ear_history))
                        new_base = float(np.mean(sorted_ear[-40:]))
                        state.baseline_ear = 0.95 * state.baseline_ear + 0.05 * new_base

                    # --- 居眠り・疲労度・姿勢判定 ---
                    ear_ratio = state.ear / (state.baseline_ear + 0.001)
                    
                    # A. 連続閉眼トラッキング
                    is_closed = (ear_ratio < 0.65)
                    if is_closed:
                        state.consecutive_closed_frames += 1
                        if state.consecutive_closed_frames > 8:
                            state.score += 1.0
                        if state.consecutive_closed_frames > 20:
                            state.score += 2.5
                        if state.consecutive_closed_frames >= 35:
                            state.score = max(state.score, 45.0)
                    else:
                        state.consecutive_closed_frames = 0
                        # 開眼時の回復減衰
                        if ear_ratio >= 0.80:
                            state.score = max(0.0, state.score - 1.5)
                        else:
                            state.score = max(0.0, state.score - 0.6)

                    # B. あくび検知 (MAR)
                    if current_mar > 0.52:
                        state.consecutive_yawn_frames += 1
                        if state.consecutive_yawn_frames == 18:
                            state.yawn_count += 1
                            state.score += 8.0
                            print(f"[YAWN] あくび検知！ 累計: {state.yawn_count}回")
                    else:
                        state.consecutive_yawn_frames = max(0, state.consecutive_yawn_frames - 1)

                    # C. 頭部姿勢のズレ
                    delta_pitch = state.pitch - state.baseline_pitch
                    delta_roll = state.roll - state.baseline_roll
                    if delta_pitch > 9.0 or abs(delta_roll) > 10.0:
                        state.score += 1.2

                    # 対話後警戒期間（POST_ALERT）の減衰管理（30秒間）
                    if state.in_post_alert:
                        if now - state.post_alert_start_time > 30.0:
                            state.in_post_alert = False

                    # スコア上限クランプ
                    state.score = float(np.clip(state.score, 0.0, 100.0))

                    # D. 状態判定 & 警告トリガー
                    if state.score > 40:
                        state.drowsiness_level = "DANGER (居眠り危険)"
                        if now - state.last_alert_time > 3.0:
                            state.last_alert_time = now
                            save_detailed_log("Emergency Alert & Buzzer Triggered")
                            threading.Thread(target=play_emergency_alert, daemon=True).start()
                            trigger_dialog()
                    elif state.score > 20 or state.consecutive_closed_frames > 15:
                        state.drowsiness_level = "WARNING (眠気・疲労予兆)"
                    elif state.in_post_alert:
                        state.drowsiness_level = "POST_ALERT (注意警戒中)"
                    else:
                        state.drowsiness_level = "NORMAL (安全)"

                    # HUD描画
                    delta_p = state.pitch - state.baseline_pitch
                    ear_pct = int((state.ear / (state.baseline_ear + 1e-4)) * 100)
                    hud_color = (0, 255, 0) if "NORMAL" in state.drowsiness_level else (
                        (0, 200, 255) if ("WARNING" in state.drowsiness_level or "POST_ALERT" in state.drowsiness_level) else (0, 0, 255)
                    )
                    
                    cv2.putText(frame, f"EAR: {state.ear:.2f} ({ear_pct}%)", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
                    cv2.putText(frame, f"MAR: {state.mar:.2f}", (15, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
                    cv2.putText(frame, f"Pitch: {delta_p:+.1f}", (15, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
                    frame = draw_text_jp(frame, f"{state.drowsiness_level}", (15, 90), JP_FONT_HUD, hud_color)

                for p in [33, 133, 362, 263, 13, 14, 78, 308]:
                    cv2.circle(frame, (int(landmarks[p].x * w), int(landmarks[p].y * h)), 2, (0, 255, 0), -1)
            else:
                # 顔ロスト（わき見・脱力）検知処理
                state.face_detected = False
                if state.face_lost_start_time == 0.0:
                    state.face_lost_start_time = now
                
                lost_duration = now - state.face_lost_start_time
                if is_calibrated and lost_duration >= 2.0:
                    state.is_distracted = True
                    state.score = min(100.0, max(state.score, 25.0) + 1.0)
                    state.drowsiness_level = "DISTRACTION (わき見・脱力注意)"
                    frame = draw_text_jp(frame, f"前方注意！ 顔未検出: {lost_duration:.1f}秒", (15, 30), JP_FONT_HUD, (0, 0, 255))
                    
                    # 4秒以上顔が見えない場合は危険判定
                    if lost_duration >= 4.0 and now - state.last_alert_time > 3.0:
                        state.last_alert_time = now
                        save_detailed_log("Distraction Alert Triggered")
                        threading.Thread(target=play_emergency_alert, daemon=True).start()
                        trigger_dialog()
                else:
                    frame = draw_text_jp(frame, "顔を検出中...", (15, 30), JP_FONT_HUD, (0, 200, 255))

            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    finally:
        state.camera_connected = False
        try:
            cap.release()
        except Exception:
            pass
