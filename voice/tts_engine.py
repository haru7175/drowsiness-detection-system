import queue
import threading
import time
import winsound
import pythoncom
import pyttsx3

tts_queue = queue.Queue()

def tts_worker():
    """専用スレッドでpyttsx3を常駐管理し、Windows COMの競合・クラッシュを防ぐ。
    万一のSAPIクラッシュ時もエンジンを再初期化して復旧する自己修復機構付き。"""
    pythoncom.CoInitialize()
    engine = None

    def create_engine():
        try:
            eng = pyttsx3.init()
            eng.setProperty('rate', 140)
            return eng
        except Exception as ex:
            print(f"TTS Init Error: {ex}")
            return None

    engine = create_engine()
    try:
        while True:
            text = tts_queue.get()
            if text is None:
                break
            try:
                if engine is None:
                    engine = create_engine()
                if engine:
                    engine.say(text)
                    engine.runAndWait()
            except Exception as ex:
                print(f"TTS Speech Error: {ex}, attempting engine recreation...")
                try:
                    engine.stop()
                except:
                    pass
                engine = create_engine()
            finally:
                tts_queue.task_done()
    finally:
        pythoncom.CoUninitialize()

# アプリ起動時にTTS専用スレッドを常駐起動
threading.Thread(target=tts_worker, daemon=True).start()

def speak_text(text, wait=True):
    """安全に音声をキューに送る。wait=Trueの場合は発話完了まで待機"""
    tts_queue.put(text)
    if wait:
        tts_queue.join()

def play_emergency_alert():
    """居眠り検知（DANGER）の瞬間に即座に高周波ブザーを鳴らす（遅延0.05秒）"""
    try:
        print("[ALERT] [EMERGENCY ALERT] 即時警告ブザー発報！")
        for _ in range(2):
            winsound.Beep(2200, 250)
            time.sleep(0.05)
    except Exception as ex:
        print(f"Emergency Buzzer Error: {ex}")
