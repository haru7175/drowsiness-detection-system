import queue
import threading
import time
import speech_recognition as sr
import ollama
from core.state import state
from voice.tts_engine import speak_text
from navigation.spot_navigator import (
    find_best_spot,
    start_navigation,
    cancel_navigation,
    is_pure_affirmation
)

dialog_queue = queue.Queue(maxsize=1)

def sanitize_ai_response(text):
    """LLMの長文やハルシネーション、前置きを切り落とし、運転に適した自然な発話（60文字以内）に整形"""
    if not text:
        return "安全な場所で休憩してくださいね。"
    
    text = text.replace('"', '').replace('「', '').replace('」', '').replace('\n', ' ').strip()
    for prefix in ["AI:", "アシスタント:", "承知しました、", "ドライバーさん、", "ご提案します。"]:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()

    sentences = [s.strip() + "。" for s in text.split("。") if s.strip()]
    if sentences:
        if len(sentences) >= 2 and len(sentences[0] + sentences[1]) <= 65:
            text = sentences[0] + sentences[1]
        else:
            text = sentences[0]
            if len(text) > 60:
                text = text[:55] + "。"
    elif len(text) > 60:
        text = text[:55] + "..."

    return text

def call_ollama_with_timeout(messages, timeout_sec=2.5):
    """Ollamaへの推論呼び出しをタイムアウト保護付きで実行"""
    result_holder = {"content": None, "error": None}

    def target():
        try:
            response = ollama.chat(
                model='qwen2.5:7b',
                messages=messages,
                options={
                    'temperature': 0.0,
                    'top_p': 0.8,
                    'repeat_penalty': 1.1,
                    'num_ctx': 512
                }
            )
            result_holder["content"] = response['message']['content'].strip()
        except Exception as e:
            result_holder["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        print(f"[WARN] Ollama call timed out ({timeout_sec}s). Using fallback.")
        return None
    if result_holder["error"]:
        print(f"[WARN] Ollama call error: {result_holder['error']}")
        return None
    return result_holder["content"]

def dialog_worker():
    """常駐スレッドで対話タスクを非同期実行"""
    while True:
        event = dialog_queue.get()
        if event is None:
            break
        try:
            ai_agent_task()
        except Exception as ex:
            print(f"Dialog Worker Error: {ex}")
        finally:
            dialog_queue.task_done()

threading.Thread(target=dialog_worker, daemon=True).start()

def trigger_dialog():
    """カメラ処理等から非同期かつ非ブロッキングに対話を要求"""
    if not state.is_talking:
        try:
            dialog_queue.put_nowait(True)
        except queue.Full:
            pass

def ai_agent_task():
    """生体状態・現在地と連動した安全第一の対話エージェント"""
    if state.is_talking:
        return
    if not state.lock.acquire(blocking=False):
        return
    
    state.is_talking = True
    try:
        # 1. センシング情報に応じた初回の質問・提案
        if state.is_distracted:
            initial_msg = "前方に注意してくださいね。少しお疲れですか？近くの休憩スポットへご案内しましょうか？"
        elif state.consecutive_closed_frames >= 20:
            initial_msg = "意識は大丈夫ですか？少しお疲れのようです。今何か食べたいものや、寄りたい休憩スポットはありますか？コーヒーやコンビニ、道の駅など案内できますよ。"
        elif state.yawn_count >= 2 or state.consecutive_yawn_frames > 0:
            initial_msg = "あくびが出ていますね。近くで寄りたい場所や食べたいものはありますか？コンビニやカフェを案内できますよ。"
        elif state.pitch - state.baseline_pitch > 9.0:
            initial_msg = "頭が下がってお疲れのようです。気分転換に何か飲みたいものや、休憩したい場所はありますか？"
        else:
            initial_msg = "お疲れのようです。近くで何か食べたいものや、寄りたい休憩スポットはありますか？"

        state.add_chat(f"[AI]: {initial_msg}")
        speak_text(initial_msg)

        r = sr.Recognizer()
        r.energy_threshold = 300
        r.dynamic_energy_threshold = True

        for turn in range(2):  # 最大2往復
            driver_text = ""
            for retry in range(2):
                try:
                    with sr.Microphone() as source:
                        print(f"DEBUG: 環境ノイズ適応 (0.4秒)... (ターン {turn+1}, 試行 {retry+1})")
                        r.adjust_for_ambient_noise(source, duration=0.4)
                        print("DEBUG: 音声入力待機中...")
                        audio = r.listen(source, timeout=3.5, phrase_time_limit=4.0)
                    
                    driver_text = r.recognize_google(audio, language='ja-JP').strip()
                    print(f"DEBUG: 認識成功 -> {driver_text}")
                    if driver_text:
                        break
                except sr.WaitTimeoutError:
                    print(f"DEBUG: 音声入力タイムアウト (試行 {retry+1}回目)")
                except sr.UnknownValueError:
                    print(f"DEBUG: 音声不明瞭 (試行 {retry+1}回目)")
                except Exception as mic_err:
                    print(f"Voice Recognition Error: {mic_err}")
                    time.sleep(0.2)

            if not driver_text:
                if turn == 0:
                    suggest_msg = "近くにコンビニやカフェ、道の駅があります。最寄りの休憩スポットへご案内しましょうか？"
                    state.add_chat(f"[AI]: {suggest_msg}")
                    speak_text(suggest_msg)
                    continue
                else:
                    spot = find_best_spot("コンビニ", state.lat, state.lng)
                    start_navigation(spot, speak_fn=speak_text, speak=False)
                    final_msg = f"安全のため、{spot['name']}へルート案内を設定しました。無理せず向かってくださいね。"
                    state.add_chat(f"[AI]: {final_msg}")
                    speak_text(final_msg)
                    break

            state.add_chat(f"[あなた]: {driver_text}")

            # キャンセル判定
            if any(word in driver_text for word in ["大丈夫", "起きた", "起きてる", "眠くない", "平気", "いらない", "結構", "やめて", "キャンセル"]):
                cancel_navigation()
                final_msg = "承知しました。無理はなさらず、目を覚まして安全運転を続けてくださいね。"
                state.add_chat(f"[AI]: {final_msg}")
                speak_text(final_msg)
                break

            # 肯定相槌判定
            if is_pure_affirmation(driver_text):
                spot = find_best_spot("コンビニ", state.lat, state.lng)
                start_navigation(spot, speak_fn=speak_text, speak=False)
                nav_msg = f"かしこまりました！{spot['name']}へ案内を開始しますね。気をつけて向かってください。"
                state.add_chat(f"[AI]: {nav_msg}")
                speak_text(nav_msg)
                break

            # ドライバーの希望スポット案内
            spot = find_best_spot(driver_text, state.lat, state.lng)
            start_navigation(spot, speak_fn=speak_text, speak=False)

            # Ollama (Qwen2.5) 生成（タイムアウト付き）
            messages = [
                {
                    'role': 'system',
                    'content': (
                        "あなたは車載の安全運転支援AIアシスタントです。\n"
                        "ドライバーは眠気を感じています。\n"
                        "【絶対厳守ルール】\n"
                        "1. 前置きは付けず、ドライバーへの発話のみを【1文または2文・60文字以内】で返答してください。\n"
                        "2. 選定された案内先へ向かうことを伝え、温かい思いやりのある言葉をかけてください。\n"
                        "3. 指定された案内先以外の架空の店名は言わず、簡潔に励ましてください。"
                    )
                },
                {
                    'role': 'user',
                    'content': f"ドライバーの発言: 「{driver_text}」\n選定された案内先: {spot['name']}（Googleマップで最寄りを案内中、{spot['desc']}）"
                }
            ]

            fallback_reply = f"かしこまりました！{spot['name']}へ案内を開始しますね。気をつけて向かってください。"
            print(f"DEBUG: Querying Ollama (qwen2.5:7b, timeout=2.5s) -> {driver_text}")
            raw_ai = call_ollama_with_timeout(messages, timeout_sec=2.5)
            if raw_ai:
                ai_text = sanitize_ai_response(raw_ai)
            else:
                ai_text = fallback_reply

            print(f"DEBUG: Received AI -> {ai_text}")
            state.add_chat(f"[AI]: {ai_text}")
            speak_text(ai_text)
            break

    except Exception as e:
        print(f"CRITICAL AI AGENT ERROR: {e}")
        fallback_msg = "最寄りの安全な場所で休憩してくださいね。"
        state.add_chat(f"[AI]: {fallback_msg}")
        try:
            speak_text(fallback_msg)
        except Exception:
            pass
    finally:
        # 重要修正: スコアを0に強制リセットせず、警戒フェーズ（POST_ALERT）へ移行
        # スコアをWARNING警戒ライン（25pt）にとどめ、開眼度・覚醒が継続すれば自然減衰させる
        state.score = min(state.score, 25.0)
        state.consecutive_closed_frames = 0
        state.in_post_alert = True
        state.post_alert_start_time = time.time()
        state.is_talking = False
        state.lock.release()
