import json
import re
import time
import urllib.parse
import urllib.request
import requests
import numpy as np
from core.state import state

# インメモリキャッシュ（Nominatim APIの制限回避・高速化・上限100件管理）
# キー: (clean_query, round(lat, 2), round(lng, 2)) -> 値: (timestamp, spot_dict)
SPOT_CACHE = {}
CACHE_TTL = 300  # 5分間有効
MAX_CACHE_SIZE = 100

def set_cache_entry(key, spot_dict):
    """キャッシュ上限管理付きエントリー保存"""
    now = time.time()
    if len(SPOT_CACHE) >= MAX_CACHE_SIZE:
        # 最も古いキーを削除
        oldest_key = min(SPOT_CACHE.keys(), key=lambda k: SPOT_CACHE[k][0])
        SPOT_CACHE.pop(oldest_key, None)
    SPOT_CACHE[key] = (now, spot_dict)

def calculate_distance_km(lat1, lon1, lat2, lon2):
    """2点間の球面距離（km）を算出"""
    R = 6371.0
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2.0)**2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2.0)**2
    c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    return float(R * c)

def extract_destination_query(text):
    """発話から目的地・店舗名・キーワードを抽出"""
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
        "をお願いします", "お願いします", "おねがいします",
        "をお願い", "お願い", "おねがい",
        "を頼む", "頼む", "頼みます", "頼むよ",
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
    """目的地名詞を含まない肯定相槌かどうか判定"""
    cleaned = extract_destination_query(text)
    pure_words = ["はい", "うん", "ええ", "そう", "そうして", "そこ", "そこにして", "いいよ", "頼む", "頼みます", "案内して", "お願い", "おねがい", "お願いします", "おねがいします"]
    return (not cleaned) or (cleaned in pure_words)

def search_real_spot_nearby(query, lat, lng):
    """OpenStreetMap / Nominatim APIを用いて周辺の実在店舗を検索（キャッシュ機構付き）"""
    cache_key = (query, round(lat, 2), round(lng, 2))
    now = time.time()
    if cache_key in SPOT_CACHE:
        c_time, c_val = SPOT_CACHE[cache_key]
        if now - c_time < CACHE_TTL:
            return c_val

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

    delta = 0.15
    params = {
        "q": search_term,
        "format": "json",
        "viewbox": f"{lng-delta},{lat+delta},{lng+delta},{lat-delta}",
        "bounded": 1,
        "limit": 5,
        "accept-language": "ja"
    }
    headers = {"User-Agent": "MobilityDriverSafetyAssistant/2.0 (mobility_new)"}
    try:
        resp = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=headers, timeout=2.0)
        if resp.status_code == 200:
            items = resp.json()
            results = []
            for item in items:
                display_name = item.get("display_name", "")
                parts = [p.strip() for p in display_name.split(",") if p.strip()]
                short_name = parts[0] if parts else search_term
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
                best = results[0]
                set_cache_entry(cache_key, best)
                return best
    except Exception:
        pass
    return None

def resolve_dynamic_spot(query_text, current_lat, current_lng):
    """発話と現在地から最適な実在スポットと案内情報を生成"""
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
        encoded_dest = urllib.parse.quote(final_spot_name)
        gmaps_nav_url = f"https://www.google.com/maps/dir/?api=1&origin={current_lat},{current_lng}&destination={encoded_dest}&travelmode=driving&dir_action=navigate"
    else:
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
    """互換関数"""
    return resolve_dynamic_spot(query_text, current_lat, current_lng)

def get_route_coordinates(start_lat, start_lng, dest_lat, dest_lng):
    """OSRM APIを用いて実際の道路沿いの経路座標リストを取得。失敗時は直線2点にフォールバック"""
    url = f"http://router.project-osrm.org/route/v1/driving/{start_lng},{start_lat};{dest_lng},{dest_lat}?overview=full&geometries=geojson"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'MobilityAI/2.0'})
        with urllib.request.urlopen(req, timeout=2.0) as res:
            if res.status == 200:
                data = json.loads(res.read().decode('utf-8'))
                if data.get('code') == 'Ok' and data.get('routes'):
                    coords = [[p[1], p[0]] for p in data['routes'][0]['geometry']['coordinates']]
                    if coords:
                        return coords
    except Exception:
        pass
    return [[start_lat, start_lng], [dest_lat, dest_lng]]

def start_navigation(spot, speak_fn=None, speak=False):
    """目的地へのナビゲーションを開始"""
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
    
    if speak and speak_fn:
        nav_speech = f"{spot['name']}へ案内を開始しますね。気をつけて向かってください。"
        state.add_chat(f"[AI]: {nav_speech}")
        speak_fn(nav_speech)

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
