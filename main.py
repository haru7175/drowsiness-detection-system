import sys
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
import json
import urllib.request
import urllib.parse
import re
import requests
from PIL import Image, ImageDraw, ImageFont

# WindowsコンソールでのUnicodeEncodeError防止
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# --- 日本語フォント設定（OpenCVの文字化け防止・Pillow描画用） ---
FONT_PATH = "C:/Windows/Fonts/meiryo.ttc"
if not os.path.exists(FONT_PATH):
    FONT_PATH = "C:/Windows/Fonts/msgothic.ttc"
try:
    jp_font_hud = ImageFont.truetype(FONT_PATH, 20)
except Exception:
    jp_font_hud = None

def draw_text_jp(frame, text, pos, font, color_bgr):
    """OpenCVのBgr画像に日本語フォントをレンダリングして返す（文字化け完全解消）"""
    if font is None:
        cv2.putText(frame, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.65, color_bgr, 2)
        return frame
    img_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    color_rgb = (int(color_bgr[2]), int(color_bgr[1]), int(color_bgr[0]))
    draw.text(pos, text, font=font, fill=color_rgb)
    return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

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
        self.camera_connected = False      # カメラ接続状態（真偽値）
        self.last_frame_time = 0.0         # 最終フレーム取得タイムスタンプ
        # テスト用の初期位置（笠松駅周辺）
        self.lat = 35.3725
        self.lng = 136.7628
        self.chat_history = []
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
        self.last_alert_time = 0.0  # 警告音の重複鳴動防止用タイムスタンプ

state = SystemState()

# --- TTS（音声合成）の常駐ワーカースレッド（Q2対応: COM競合防止・自動自己修復） ---
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
    """安全に音声をキューに送る。wait=Trueの場合は発話完了まで待機（マイクの誤拾い防止）"""
    tts_queue.put(text)
    if wait:
        tts_queue.join()

# --- 即時緊急警告ブザー（遅延ゼロで運転手を覚醒） ---
def play_emergency_alert():
    """居眠り検知（DANGER）の瞬間に即座に高周波ブザーを鳴らす"""
    try:
        print("[ALERT] [EMERGENCY ALERT] 即時警告ブザー発報！")
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

# --- 4. データベースと非同期ロギング機能（Q1対応: カメラループのディスクI/Oブロッキング防止） ---
log_queue = queue.Queue()

def log_worker():
    """SQLiteへの書き込みを専用ワーカースレッドで直列処理し、カメラ処理のFPS低下をゼロにする"""
    conn = sqlite3.connect('mobility_advanced.db', check_same_thread=False)
    while True:
        log_entry = log_queue.get()
        if log_entry is None:
            break
        t, score, ear, pitch, roll, lat, lng, action = log_entry
        try:
            c = conn.cursor()
            c.execute("INSERT INTO detailed_logs (timestamp, score, ear, pitch, roll, lat, lng, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (t, score, ear, pitch, roll, lat, lng, action))
            conn.commit()
        except Exception as ex:
            print(f"Async DB Log Error: {ex}")
        finally:
            log_queue.task_done()

threading.Thread(target=log_worker, daemon=True).start()

def init_db():
    conn = sqlite3.connect('mobility_advanced.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles (name TEXT PRIMARY KEY, baseline_ear REAL, avg_blink_rate REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS detailed_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, score REAL, ear REAL, pitch REAL, roll REAL, lat REAL, lng REAL, action TEXT)''')
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

# --- 5. 全国・全世界対応 動的スポット解決＆Google Mapsナビゲーションエンジン ---

def extract_destination_query(text):
    """
    発話から「行きたい」「案内して」「寄りたい」などの語尾や不要表現を除去し、
    正確な目的地・店舗名・キーワードを抽出する
    """
    q = text.strip() if text else ""
    q = re.sub(r"[。、！？!\?]+", "", q).strip()
    
    # 先頭の不要修飾語
    prefixes = ["近くの", "最寄りの", "周辺の", "どっかの", "どこかの", "すぐの", "ここから近い"]
    for p in prefixes:
        if q.startswith(p):
            q = q[len(p):].strip()
            
    # 末尾の語尾・動詞・助詞
    suffixes = [
        "に案内して", "へ案内して", "案内して",
        "に連れてって", "へ連れてって", "連れてって",
        "に行きたいな", "へ行きたいな", "行きたいな",
        "に行きたい", "へ行きたい", "行きたい",
        "に行こう", "へ行こう", "行こう",
        "に寄りたい", "へ寄りたい", "寄りたい",
        "で休みたい", "休みたい",
        "で寝たい", "寝たい",
        "で仮眠したい", "仮眠したい",
        "が飲みたい", "飲みたい",
        "が食べたい", "食べたい",
        "を買いたい", "買いたい",
        "を探して", "探して",
        "をお願い", "お願い", "おねがい",
        "を頼む", "頼む",
        "にして", "にしよう", "で頼む",
        "に行け", "へ行け", "行け"
    ]
    for s in suffixes:
        if q.endswith(s):
            q = q[:-len(s)].strip()
            break
            
    # 残った末尾の助詞
    if q.endswith("に") or q.endswith("へ") or q.endswith("で") or q.endswith("を") or q.endswith("の"):
        q = q[:-1].strip()

    return q

def is_pure_affirmation(text):
    """
    「はい」「うん」「お願い」「案内して」など、
    目的地名詞を含まない純粋な肯定相槌かどうかを判定する
    """
    cleaned = extract_destination_query(text)
    pure_words = ["はい", "うん", "ええ", "そう", "そうして", "そこ", "そこにして", "いいよ", "頼む", "案内して", "お願い", "おねがい"]
    return (not cleaned) or (cleaned in pure_words)

def search_real_spot_nearby(query, lat, lng):
    """
    OpenStreetMap / Nominatim APIを用いて現在地周辺の実在する店舗（店名・緯度経度・距離）をリアルタイム検索。
    タイムアウト時は安全にNoneを返してフォールバック。
    """
    # 略称・通称から正式検索名への展開
    alias_map = {
        "スタバ": "スターバックス",
        "マック": "マクドナルド",
        "マクド": "マクドナルド",
        "セブン": "セブン-イレブン",
        "ファミマ": "ファミリーマート",
        "ローソン": "ローソン",
        "コメダ": "コメダ珈琲店",
        "ドトール": "ドトールコーヒー",
        "すき家": "すき家",
        "吉野家": "吉野家",
        "松屋": "松屋",
        "丸亀": "丸亀製麺",
        "はなまる": "はなまるうどん",
        "モス": "モスバーガー",
        "イオン": "イオン",
        "道の駅": "道の駅",
        "ガソスタ": "ガソリンスタンド",
        "スタンド": "ガソリンスタンド",
    }
    search_term = alias_map.get(query, query)

    # 検索範囲: 約15km四方 (delta = 0.15度)
    delta = 0.15
    params = {
        "q": search_term,
        "format": "json",
        "viewbox": f"{lng-delta},{lat+delta},{lng+delta},{lat-delta}",
        "bounded": 1,
        "limit": 5,
        "accept-language": "ja"
    }
    headers = {"User-Agent": "MobilityDriverSafetyAssistant/1.0"}
    try:
        resp = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=headers, timeout=2.0)
        if resp.status_code == 200:
            items = resp.json()
            results = []
            for item in items:
                display_name = item.get("display_name", "")
                parts = [p.strip() for p in display_name.split(",") if p.strip()]
                short_name = parts[0] if parts else search_term
                # 単なる「イオン」「スターバックス」等で支店名が含まれていない場合、近傍地名・支店名を補完
                if len(parts) >= 2 and len(short_name) <= 8 and not any(x in short_name for x in ["店", "モール", "支店", "PA", "SA"]):
                    sub_name = parts[1].split("（")[0].replace("(", "").replace(")", "").strip()
                    if sub_name and not sub_name.isdigit():
                        short_name = f"{short_name}（{sub_name}）"

                t_lat = float(item.get("lat"))
                t_lng = float(item.get("lon"))
                dist = calculate_distance_km(lat, lng, t_lat, t_lng)
                results.append({
                    "name": short_name,
                    "lat": t_lat,
                    "lng": t_lng,
                    "distance_km": round(dist, 2)
                })
            if results:
                results.sort(key=lambda x: x["distance_km"])
                return results[0]
    except Exception as e:
        pass
    return None

def resolve_dynamic_spot(query_text, current_lat, current_lng):
    """
    ドライバーの自由な発話（例：「イオンに案内して」「イオンに行きたい」「近くのスタバ」「ラーメン食べたい」等）から、
    正確な目的地・店舗名をクレンジングし、走行中の現在地（GPS）から最寄りの実在店舗をリアルタイム検索して
    正確な店名とGoogle Mapsナビゲーション情報を生成する。
    """
    raw_query = query_text.strip() if query_text else ""
    cleaned_query = extract_destination_query(raw_query)
    if not cleaned_query:
        cleaned_query = "休憩スポット"

    q_lower = cleaned_query.lower()
    cat = "general"
    cat_name = "休憩スポット"
    dest_keyword = cleaned_query
    desc = f"ご希望の「{cleaned_query}」でリフレッシュできます。"

    # ジャンル・カテゴリ判定
    if any(k in q_lower for k in ["イオン", "イオンモール", "aeon"]):
        cat = "shopping"
        cat_name = "ショッピングモール"
        dest_keyword = "イオン"
        desc = "ショッピングモールで休憩や買い物、食事ができます。"
    elif any(k in q_lower for k in ["コーヒー", "珈琲", "カフェ", "喫茶", "カフェラテ", "ラテ", "スタバ", "スターバックス", "コメダ", "ドトール"]):
        cat = "cafe"
        cat_name = "カフェ・喫茶"
        dest_keyword = "スターバックス" if "スタバ" in q_lower else ("コメダ珈琲店" if "コメダ" in q_lower else ("ドトールコーヒー" if "ドトール" in q_lower else "カフェ"))
        desc = "淹れたてのコーヒーでカフェインをチャージし、リフレッシュできます。"
    elif any(k in q_lower for k in ["コンビニ", "セブン", "ローソン", "ファミマ", "ファミリーマート", "ミニストップ"]):
        cat = "convenience"
        cat_name = "コンビニ"
        dest_keyword = "セブン-イレブン" if "セブン" in q_lower else ("ローソン" if "ローソン" in q_lower else ("ファミリーマート" if "ファミマ" in q_lower else "コンビニ"))
        desc = "冷たい飲み物や軽食、淹れたてコーヒーで素早くリフレッシュできます。"
    elif any(k in q_lower for k in ["道の駅", "仮眠", "寝たい", "休む", "休憩", "パーキング", "pa", "sa", "サービスエリア", "横になりたい", "眠い"]):
        cat = "rest"
        cat_name = "道の駅・休憩所"
        dest_keyword = "道の駅" if "道の駅" in q_lower else ("パーキングエリア" if any(x in q_lower for x in ["pa", "sa", "パーキング"]) else "道の駅")
        desc = "安全に車を停めて、しっかり仮眠・休息を取ることができます。"
    elif any(k in q_lower for k in ["ラーメン", "らーめん", "拉麺", "タンメン", "中華"]):
        cat = "food"
        cat_name = "ラーメン・食事"
        dest_keyword = "ラーメン"
        desc = "温かいスープと麺でエネルギーを補給できます。"
    elif any(k in q_lower for k in ["うどん", "そば", "蕎麦", "丸亀", "はなまる"]):
        cat = "food"
        cat_name = "うどん・蕎麦"
        dest_keyword = "うどん" if "うどん" in q_lower else "そば"
        desc = "出汁の効いた温かい麺類でほっと一息つけます。"
    elif any(k in q_lower for k in ["マック", "マクド", "マクドナルド", "バーガー", "ハンバーガー", "モス"]):
        cat = "food"
        cat_name = "ファストフード"
        dest_keyword = "モスバーガー" if "モス" in q_lower else "マクドナルド"
        desc = "ドライブスルーや軽食で手軽にエネルギー補給できます。"
    elif any(k in q_lower for k in ["牛丼", "すき家", "吉野家", "松屋"]):
        cat = "food"
        cat_name = "牛丼・定食"
        dest_keyword = "すき家" if "すき家" in q_lower else ("吉野家" if "吉野家" in q_lower else ("松屋" if "松屋" in q_lower else "牛丼"))
        desc = "手早くがっつり食べて活力を取り戻せます。"
    elif any(k in q_lower for k in ["ガソリン", "給油", "スタンド", "eneos", "出光"]):
        cat = "service"
        cat_name = "ガソリンスタンド"
        dest_keyword = "ガソリンスタンド"
        desc = "給油と合わせて洗車や空気圧チェック、休憩ができます。"
    elif any(k in q_lower for k in ["甘いもの", "スイーツ", "アイス", "デザート"]):
        cat = "cafe"
        cat_name = "スイーツ・甘味"
        dest_keyword = "スイーツ カフェ"
        desc = "糖分補給で疲れた脳と身体をリフレッシュできます。"
    elif any(k in q_lower for k in ["ご飯", "ごはん", "食事", "ランチ", "お腹", "ファミレス", "定食", "レストラン", "肉"]):
        cat = "food"
        cat_name = "レストラン・食事"
        dest_keyword = "レストラン"
        desc = "しっかりした食事でスタミナを回復できます。"

    # 現在地周辺の実在店舗をリアルタイム検索（実店舗名・正確な緯度経度・正確な距離を取得）
    real_spot = search_real_spot_nearby(dest_keyword, current_lat, current_lng)

    if real_spot:
        final_spot_name = real_spot['name']
        final_lat = real_spot['lat']
        final_lng = real_spot['lng']
        dist_km = real_spot['distance_km']
        if dist_km < 1.0:
            dist_text = f"約{int(dist_km * 1000)}m"
            eta_mins = max(1, int(dist_km * 1000 / 300))
        else:
            dist_text = f"約{dist_km:.1f}km"
            eta_mins = max(2, int(dist_km * 2))
        eta_text = f"車で約{eta_mins}分"
        # 正確な実在店舗名を目的地としてGoogle Maps Universal URLを生成！
        encoded_dest = urllib.parse.quote(final_spot_name)
        gmaps_nav_url = f"https://www.google.com/maps/dir/?api=1&origin={current_lat},{current_lng}&destination={encoded_dest}&travelmode=driving&dir_action=navigate"
    else:
        # フォールバック（クレンジングされたキーワードを使用）
        final_spot_name = f"近くの{dest_keyword}"
        final_lat = current_lat
        final_lng = current_lng
        dist_text = "最寄り"
        eta_text = "車で約3〜5分"
        encoded_dest = urllib.parse.quote(dest_keyword)
        gmaps_nav_url = f"https://www.google.com/maps/dir/?api=1&origin={current_lat},{current_lng}&destination={encoded_dest}&travelmode=driving&dir_action=navigate"

    return {
        "id": f"dyn_{cat}",
        "name": final_spot_name,
        "destination_query": dest_keyword,
        "category": cat,
        "category_name": cat_name,
        "lat": final_lat,
        "lng": final_lng,
        "distance_text": dist_text,
        "eta_text": eta_text,
        "desc": desc,
        "gmaps_url": gmaps_nav_url
    }

def find_best_spot(query_text, current_lat, current_lng):
    """従来の互換性を維持しつつ、全国どこでもあらゆる店舗に対応する動的スポット解決エンジンを呼び出す"""
    return resolve_dynamic_spot(query_text, current_lat, current_lng)

def calculate_distance_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2.0)**2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2.0)**2
    c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    return float(R * c)

def get_route_coordinates(start_lat, start_lng, dest_lat, dest_lng):
    """OSRM APIを用いて実際の道路沿いの経路座標リストを取得。失敗時は直線2点にフォールバック"""
    url = f"http://router.project-osrm.org/route/v1/driving/{start_lng},{start_lat};{dest_lng},{dest_lat}?overview=full&geometries=geojson"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'MobilityAI/1.0'})
        with urllib.request.urlopen(req, timeout=2.0) as res:
            if res.status == 200:
                data = json.loads(res.read().decode('utf-8'))
                if data.get('code') == 'Ok' and data.get('routes'):
                    coords = [[p[1], p[0]] for p in data['routes'][0]['geometry']['coordinates']]
                    if coords:
                        return coords
    except Exception as e:
        pass
    return [[start_lat, start_lng], [dest_lat, dest_lng]]

def start_navigation(spot, speak=False):
    """目的地へのナビゲーションを開始し、Google Maps / 走行ルート情報をシステム状態に設定"""
    route = get_route_coordinates(state.lat, state.lng, spot.get('lat', state.lat), spot.get('lng', state.lng))
    dest_query = spot.get('destination_query', spot.get('name', '休憩所'))
    encoded_dest = urllib.parse.quote(dest_query)
    gmaps_nav_url = spot.get('gmaps_url') or f"https://www.google.com/maps/dir/?api=1&origin={state.lat},{state.lng}&destination={encoded_dest}&travelmode=driving&dir_action=navigate"

    state.navigation = {
        "active": True,
        "spot_name": spot['name'],
        "destination_query": dest_query,
        "category": spot.get('category', 'general'),
        "category_name": spot.get('category_name', '休憩スポット'),
        "lat": spot.get('lat', state.lat),
        "lng": spot.get('lng', state.lng),
        "distance_text": spot.get('distance_text', '最寄り'),
        "eta_text": spot.get('eta_text', '車で約3〜5分'),
        "desc": spot.get('desc', ''),
        "route": route,
        "gmaps_url": gmaps_nav_url
    }
    state.suggested_spot = f"{spot['name']}"
    print(f"[NAV] ナビゲーション開始: {spot['name']} (検索キーワード: {dest_query})")
    if speak:
        nav_speech = f"{spot['name']}へ案内を開始しますね。気をつけて向かってください。"
        state.chat_history.append(f"[AI]: {nav_speech}")
        speak_text(nav_speech)

def cancel_navigation():
    """ナビゲーションを終了・クリア"""
    state.navigation = {
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
    state.suggested_spot = "安全運転継続中"
    print("[NAV] ナビゲーションを終了しました")

# --- 6. マルチモーダル対話エージェント（提案・案内連動版） ---
def sanitize_ai_response(text):
    """LLMの長文やハルシネーション、前置きを切り落とし、運転に適した自然な発話（60文字以内）に整形"""
    if not text:
        return "安全な場所で休憩してくださいね。"
    
    text = text.replace('"', '').replace('「', '').replace('」', '').replace('\n', ' ').strip()
    for prefix in ["AI:", "アシスタント:", "承知しました、", "ドライバーさん、", "ご提案します。"]:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()

    # 最大2文または60文字以内で抽出
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

# --- 対話エージェント専用ワーカースレッド（Q1対応: メインスレッド・カメラFPSの保護） ---
dialog_queue = queue.Queue(maxsize=1)

def dialog_worker():
    """常駐スレッドで対話タスクを非同期実行し、カメラループからの同期ブロックを完全防止"""
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
    """カメラ処理等から非同期かつ非ブロッキングに対話を要求（キュー満杯時は安全にスキップ）"""
    if not state.is_talking:
        try:
            dialog_queue.put_nowait(True)
        except queue.Full:
            pass

def ai_agent_task():
    """ドライバーの生体状態・現在地と連動した安全第一の提案・案内型対話エージェント"""
    if state.is_talking: return
    if not state.lock.acquire(blocking=False): return
    
    state.is_talking = True
    try:
        # 1. センシング情報に応じた初回の質問・提案（一方的な命令ではなく、希望を質問する！）
        if state.consecutive_closed_frames >= 20:
            initial_msg = "意識は大丈夫ですか？少しお疲れのようです。今何か食べたいものや、寄りたい休憩スポットはありますか？コーヒーやコンビニ、道の駅など案内できますよ。"
        elif state.yawn_count >= 2 or state.consecutive_yawn_frames > 0:
            initial_msg = "あくびが出ていますね。近くで寄りたい場所や食べたいものはありますか？コンビニやカフェを案内できますよ。"
        elif state.pitch - state.baseline_pitch > 9.0:
            initial_msg = "頭が下がってお疲れのようです。気分転換に何か飲みたいものや、休憩したい場所はありますか？"
        else:
            initial_msg = "お疲れのようです。近くで何か食べたいものや、寄りたい休憩スポットはありますか？"

        state.chat_history.append(f"[AI]: {initial_msg}")
        speak_text(initial_msg)

        r = sr.Recognizer()
        r.energy_threshold = 300
        r.dynamic_energy_threshold = True

        for turn in range(2): # 最大2往復
            driver_text = ""
            timed_out = False
            for retry in range(2):
                try:
                    with sr.Microphone() as source:
                        print(f"DEBUG: 環境ノイズ適応測定中 (0.5秒)... (ターン {turn+1}, 試行 {retry+1})")
                        r.adjust_for_ambient_noise(source, duration=0.5)
                        print("DEBUG: 音声入力待機中...")
                        audio = r.listen(source, timeout=4.0, phrase_time_limit=4.5)
                    
                    driver_text = r.recognize_google(audio, language='ja-JP').strip()
                    print(f"DEBUG: 認識成功 -> {driver_text}")
                    if driver_text:
                        break
                except sr.WaitTimeoutError:
                    print(f"DEBUG: 音声入力タイムアウト（無言） (試行 {retry+1}回目)")
                    timed_out = True
                except sr.UnknownValueError:
                    print(f"DEBUG: 音声不明瞭（環境音など） (試行 {retry+1}回目)")
                    timed_out = False
                except Exception as mic_err:
                    print(f"Voice Recognition Error Retry {retry+1}: {mic_err}")
                    timed_out = False
                    time.sleep(0.3)

            # 音声認識できなかった場合の親切な提案（いきなり打ち切らず、具体的な選択肢を提案！）
            if not driver_text:
                if turn == 0:
                    suggest_msg = "近くにコンビニやカフェ、道の駅があります。最寄りの休憩スポットへご案内しましょうか？"
                    state.chat_history.append(f"[AI]: {suggest_msg}")
                    speak_text(suggest_msg)
                    continue  # ドライバーの返答をもう1度待つ！
                else:
                    # 2回目も無言だった場合：安全第一で最寄りの休憩スポットへ自動でルート案内を設定
                    spot = find_best_spot("コンビニ", state.lat, state.lng)
                    start_navigation(spot, speak=False)
                    final_msg = f"安全のため、{spot['name']}へルート案内を設定しました。無理せず向かってくださいね。"
                    state.chat_history.append(f"[AI]: {final_msg}")
                    speak_text(final_msg)
                    break

            state.chat_history.append(f"[あなた]: {driver_text}")

            # キャンセル判定（音声で安全運転継続・案内解除）
            if any(word in driver_text for word in ["大丈夫", "起きた", "起きてる", "眠くない", "平気", "いらない", "結構", "やめて", "キャンセル"]):
                cancel_navigation()
                final_msg = "承知しました。無理はなさらず、目を覚まして安全運転を続けてくださいね。"
                state.chat_history.append(f"[AI]: {final_msg}")
                speak_text(final_msg)
                break

            # 肯定返答（「はい」「うん」「お願い」「案内して」など、目的地名詞を含まない単独の肯定相槌の場合のみ直前提案またはコンビニへ案内）
            if is_pure_affirmation(driver_text):
                spot = find_best_spot("コンビニ", state.lat, state.lng)
                start_navigation(spot, speak=False)
                nav_msg = f"かしこまりました！{spot['name']}へ案内を開始しますね。気をつけて向かってください。"
                state.chat_history.append(f"[AI]: {nav_msg}")
                speak_text(nav_msg)
                break

            # ドライバーの希望（食べたいもの・行きたい場所）に応じたスポット選定＆案内開始！
            spot = find_best_spot(driver_text, state.lat, state.lng)
            start_navigation(spot, speak=False)

            # Ollama (Qwen2.5) による案内メッセージの生成
            messages = [
                {
                    'role': 'system',
                    'content': (
                        "あなたは車載の安全運転支援AIアシスタントです。\n"
                        "ドライバーは眠気を感じています。\n"
                        "【絶対厳守ルール】\n"
                        "1. 前置き（「AI:」「承知しました」等）は付けず、ドライバーへの発話のみを【1文または2文・60文字以内】で返答してください。\n"
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
            try:
                print(f"DEBUG: Querying Ollama (qwen2.5:7b, temp=0.0) -> {driver_text}")
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
                ai_text = response['message']['content'].strip()
                ai_text = sanitize_ai_response(ai_text)
            except Exception as llm_err:
                print(f"Ollama Call Notice: {llm_err}")
                ai_text = fallback_reply

            print(f"DEBUG: Received AI -> {ai_text}")
            state.chat_history.append(f"[AI]: {ai_text}")
            speak_text(ai_text)
            break # 案内を開始したので対話ループ完了

    except Exception as e:
        print(f"CRITICAL AI AGENT ERROR: {e}")
        fallback_msg = "最寄りの安全な場所で休憩してくださいね。"
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
def get_camera_capture():
    """Windows環境で高速・安定してカメラをオープンする（DirectShow優先 & インデックスフォールバック）"""
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
    mp_face_mesh = mp.solutions.face_mesh
    face_mesh = mp_face_mesh.FaceMesh(max_num_faces=1, refine_landmarks=True, min_detection_confidence=0.5, min_tracking_confidence=0.5)
    
    cap = get_camera_capture()
    if cap is None:
        state.camera_connected = False
        blank = np.zeros((480, 640, 3), dtype=np.uint8)
        blank = draw_text_jp(blank, "カメラを検出できませんでした", (150, 220), jp_font_hud, (0, 0, 255))
        blank = draw_text_jp(blank, "USB接続・アクセス権限を確認してください", (120, 260), jp_font_hud, (200, 200, 200))
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
    
    calibration_frames = 0
    is_calibrated = False

    try:
        while True:
            success, frame = cap.read()
            if not success:
                state.camera_connected = False
                time.sleep(0.1)
                break
            
            # カメラ接続中フラグ & タイムスタンプ更新
            state.camera_connected = True
            state.last_frame_time = time.time()
            
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
                            print(f"[YAWN] あくび検知！ 累計: {state.yawn_count}回")
                    else:
                        state.consecutive_yawn_frames = max(0, state.consecutive_yawn_frames - 1)

                    # --- C. 頭部姿勢の平常時からのズレ判定（うなずき・脱力） ---
                    delta_pitch = state.pitch - state.baseline_pitch
                    delta_roll = state.roll - state.baseline_roll
                    if delta_pitch > 9.0 or abs(delta_roll) > 10.0:
                        state.score += 1.2

                    # --- スコア上限クランプ（0〜100に厳格に制限し、100超えのバグを完全防止） ---
                    state.score = float(np.clip(state.score, 0.0, 100.0))

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
                            # ② Q1対応: 対話ワーカーキューに非同期投入（カメラのFPS低下を完全防止）
                            trigger_dialog()
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
                    # 日本語フォント（メイリオ）で文字化けなく綺麗に描画
                    frame = draw_text_jp(frame, f"{state.drowsiness_level}", (15, 90), jp_font_hud, hud_color)

                # 目と口の特徴点ガイドを描画
                for p in [33, 133, 362, 263, 13, 14, 78, 308]:
                    cv2.circle(frame, (int(landmarks[p].x * w), int(landmarks[p].y * h)), 2, (0, 255, 0), -1)
            else:
                # 顔が検出されない場合の案内表示
                frame = draw_text_jp(frame, "顔を検出中...", (15, 30), jp_font_hud, (0, 200, 255))

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
        except:
            pass

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
    # 直近2秒以内にフレーム更新があるかで稼働状態を判定
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
    start_navigation(spot, speak=data.get('speak', True))
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
        state.chat_history.append(f"[あなた]: {driver_text}")
        if any(word in driver_text for word in ["大丈夫", "起きた", "眠くない", "平気", "キャンセル"]):
            reply = "承知しました。目を覚まして安全運転を続けてくださいね。"
            cancel_navigation()
        elif is_pure_affirmation(driver_text):
            spot = find_best_spot("コンビニ", state.lat, state.lng)
            start_navigation(spot, speak=False)
            reply = f"かしこまりました！{spot['name']}へ案内を開始しますね。気をつけて向かってください。"
        else:
            spot = find_best_spot(driver_text, state.lat, state.lng)
            start_navigation(spot, speak=False)
            reply = f"かしこまりました！{spot['name']}へ案内を開始しますね。気をつけて向かってください。"
        state.chat_history.append(f"[AI]: {reply}")
        speak_text(reply)

    threading.Thread(target=process_say, args=(text,), daemon=True).start()
    return jsonify({"status": "success"})

@app.route('/api/trigger_alert', methods=['POST'])
def api_trigger_alert():
    """テスト用に即時警告＆提案対話を起動"""
    threading.Thread(target=play_emergency_alert, daemon=True).start()
    trigger_dialog()
    return jsonify({"status": "success"})

# --- 8. メイン実行 ---
if __name__ == '__main__':
    init_db()
    print("===================================================")
    print("[START] マルチモーダル対話型 AIダッシュボード 起動準備完了！")
    print("===================================================")
    
    def open_browser():
        time.sleep(2)
        webbrowser.open('http://127.0.0.1:5000')

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='127.0.0.1', port=5000, threaded=True, debug=False)