import os
import sys
import cv2
import numpy as np
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

# 日本語フォント設定（OpenCV文字化け解消用）
FONT_PATH = "C:/Windows/Fonts/meiryo.ttc"
if not os.path.exists(FONT_PATH):
    FONT_PATH = "C:/Windows/Fonts/msgothic.ttc"

try:
    JP_FONT_HUD = ImageFont.truetype(FONT_PATH, 20)
    JP_FONT_HUD_SM = ImageFont.truetype(FONT_PATH, 16)
except Exception:
    JP_FONT_HUD = None
    JP_FONT_HUD_SM = None

def draw_text_jp(frame, text, pos, font=JP_FONT_HUD, color_bgr=(255, 255, 255)):
    """OpenCVのBGR画像に日本語フォントをレンダリングして返す"""
    if font is None:
        cv2.putText(frame, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.65, color_bgr, 2)
        return frame
    img_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    color_rgb = (int(color_bgr[2]), int(color_bgr[1]), int(color_bgr[0]))
    draw.text(pos, text, font=font, fill=color_rgb)
    return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

# 定数
DEFAULT_LAT = 35.3725
DEFAULT_LNG = 136.7628
DATABASE_NAME = "mobility_advanced.db"
