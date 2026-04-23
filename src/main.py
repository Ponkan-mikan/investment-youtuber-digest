"""
Investment YouTube Daily Digest Generator
毎日の投資系YouTube動画を要約してHTMLレポートを生成する
"""
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
import yfinance as yf

# ---- 定数 ----------------------------------------------------------------
JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = BASE_DIR / "docs"

# 何時間以内の動画を対象にするか（デフォルト48h = 投稿漏れを防ぐため少し余裕を持たせる）
LOOKBACK_HOURS = 48

# ---- チャンネルID解決 ---------------------------------------------------

def get_channel_id_from_handle(handle: str) -> str | None:
    """YouTubeチャンネルの@ハンドルからチャンネルIDを取得する。"""
    url = f"https://www.youtube.com/@{handle}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        # ページソースからチャンネルIDを正規表現で抽出
        for pattern in [
            r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
            r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
            r'"browseId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
        ]:
            m = re.search(pattern, resp.text)
            if m:
                return m.group(1)
    except Exception as e:
        print(f"  [WARN] チャンネルID取得失敗 @{handle}: {e}")
    return None


def load_channels() -> list[dict]:
    path = DATA_DIR / "channels.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_channels(channels: list[dict]) -> None:
    path = DATA_DIR / "channels.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(channels, f, indent=2, ensure_ascii=False)


def resolve_channel_ids(channels: list[dict]) -> list[dict]:
    """channel_id が null のチャンネルを解決してキャッシュする。"""
    updated = False
    for ch in channels:
        if not ch.get("channel_id"):
            print(f"チャンネルID解決中: {ch['name']} (@{ch['handle']}) ...")
            cid = get_channel_id_from_handle(ch["handle"])
            if cid:
                print(f"  → {cid}")
                ch["channel_id"] = cid
                updated = True
            else:
                print(f"  → 取得失敗（スキップ）")
            time.sleep(1.5)  # YouTube へのリクエスト間隔
    if updated:
        save_channels(channels)
        print("channels.json を更新しました。\n")
    return channels


# ---- RSS フィード -------------------------------------------------------

def get_recent_videos(channel_id: str, hours: int = LOOKBACK_HOURS) -> list[dict]:
    """RSS フィードから直近 `hours` 時間以内の動画を取得する。"""
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        resp = requests.get(rss_url, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"  [WARN] RSS取得失敗 (channel_id={channel_id}): {e}")
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    results = []
    for entry in feed.entries:
        if not hasattr(entry, "published_parsed") or entry.published_parsed is None:
            continue
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if published >= cutoff:
            # duration を media_content から取得（ショート判定に使用）
            duration = 0
            mc = entry.get("media_content", [])
            if mc and isinstance(mc, list):
                try:
                    duration = int(mc[0].get("duration", 0) or 0)
                except (ValueError, TypeError):
                    pass
            results.append({
                "video_id":         entry.get("yt_videoid", ""),
                "title":            entry.get("title", ""),
                "url":              entry.get("link", ""),
                "published":        published.isoformat(),
                "description":      entry.get("summary", ""),
                "duration_seconds": duration,
            })
    return results


# ---- ショート判定 -------------------------------------------------------

_SHORT_RE = re.compile(r'#shorts?\b|#reels?\b', re.IGNORECASE)

def is_short(video: dict) -> bool:
    """YouTube ショート動画かどうかを判定する。"""
    if "/shorts/" in video.get("url", ""):
        return True
    if _SHORT_RE.search(f"{video['title']} {video['description']}"):
        return True
    d = video.get("duration_seconds", 0)
    if d and 0 < d < 61:
        return True
    return False


# ---- 言語フィルタ -------------------------------------------------------

_DE_CHARS = re.compile(r'[äöüÄÖÜß]')
_DE_WORDS = re.compile(
    r'\b(und|nicht|auch|auf|wird|werden|Aktien|Börse|Markt|Depot|Dividende|Rendite)\b'
)

def is_german(video: dict) -> bool:
    """タイトル・概要欄がドイツ語コンテンツかどうかを判定する。"""
    text = f"{video['title']} {video['description'][:300]}"
    if _DE_CHARS.search(text):
        return True
    return len(_DE_WORDS.findall(text)) >= 2


# ---- 日時フォーマット ---------------------------------------------------

def format_pub_datetime(iso_str: str) -> str:
    """UTC ISO文字列をJST日時文字列に変換する。"""
    try:
        return datetime.fromisoformat(iso_str).astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    except Exception:
        return iso_str[:10]


# ---- 株価取得 -----------------------------------------------------------

def get_price_on_date(ticker: str, date_str: str) -> float | None:
    """指定日の終値を取得する（市場休日の場合は直近の終値）。"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        start = (dt - timedelta(days=5)).strftime("%Y-%m-%d")
        end   = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
        hist = yf.Ticker(ticker).history(start=start, end=end)
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        return None


def get_current_price(ticker: str) -> float | None:
    """現在の株価（最新終値）を取得する。"""
    try:
        hist = yf.Ticker(ticker).history(period="2d")
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        return None


# ---- Claude API 要約 ---------------------------------------------------

SUMMARY_PROMPT_TEMPLATE = """\
以下の投資系YouTube動画から、個人投資家にとって重要な情報を抽出してください。

タイトル: {title}

概要欄:
{description}

以下のJSON形式のみで回答してください（マークダウンのコードブロックや説明文は不要。JSONオブジェクトだけを出力）:
{{
  "summary_ja": "動画の主要な内容を日本語で3〜5文で要約。企業名・銘柄名・ティッカーシンボルが出てきた場合は必ず <b>企業名</b> のように <b> タグで囲むこと",
  "tickers": ["言及された銘柄の正式なティッカーシンボルのリスト（例: AAPL, NVDA, ACHR, MNDY）。会社名で言及されている場合も必ずティッカーシンボルに変換すること（例: 'Archer Aviation'→'ACHR', 'Monday.com'→'MNDY', 'Tesla'→'TSLA'）。ティッカーシンボルが不明な場合や株式銘柄でない場合は含めない。なければ空配列"],
  "undervalued_picks": ["YouTuberが明示的に『割安』『過小評価されている』『市場に無視されている』『買い場』と述べていた銘柄のティッカーシンボルのリスト。なければ空配列"],
  "key_points": ["投資判断に役立つ重要ポイントを日本語で箇条書き（最大5つ）"],
  "sentiment": "bullish か bearish か neutral のいずれか",
  "topics": ["該当するカテゴリ（例: 個別銘柄分析, マクロ経済, 投資戦略, 決算分析, 市場見通し）"],
  "importance": 投資家にとっての重要度スコア（1〜5の整数。5が最重要、投資に無関係なら1）
}}"""


_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODEL = "llama-3.3-70b-versatile"


def summarize_video(
    api_key: str,
    title: str,
    description: str,
) -> dict:
    """Groq API（Llama 3.3 70B）を使って動画の投資情報を要約する。"""
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        title=title,
        description=description[:2000],
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": _GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
        "temperature": 0.2,
    }
    # 最大3回リトライ（429 = レート制限時は待機）
    resp = None
    for attempt in range(3):
        try:
            resp = requests.post(_GROQ_URL, headers=headers, json=payload, timeout=30)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Groq API 接続エラー: {e}")
        if resp.status_code == 429:
            wait = 20 * (attempt + 1)
            print(f"  [WARN] レート制限 (429)。{wait}秒待機... (attempt {attempt+1}/3)")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break

    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"Groq API: {resp.status_code if resp else 'no response'}")

    text = resp.json()["choices"][0]["message"]["content"]

    # マークダウンコードブロックを除去
    text_clean = re.sub(r"```(?:json)?\s*", "", text)
    text_clean = re.sub(r"\s*```", "", text_clean).strip()

    # JSON ブロックを抽出
    m = re.search(r"\{[\s\S]*\}", text_clean)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError as e:
            print(f"  [WARN] JSON パース失敗: {e}")
            print(f"  [DEBUG] モデル出力先頭200字: {text[:200]!r}")

    # summary_ja だけでも取り出せるか試みる
    sm = re.search(r'"summary_ja"\s*:\s*"((?:[^"\\]|\\.)*)"', text_clean)
    summary = sm.group(1) if sm else "要約の解析に失敗しました。"
    print(f"  [WARN] JSONパース失敗のため部分フォールバック: {summary[:60]}")
    return {
        "summary_ja": summary,
        "tickers": [],
        "key_points": [],
        "sentiment": "neutral",
        "topics": [],
        "importance": 1,
    }


# ---- HTML 生成 ---------------------------------------------------------

_SENT_LABEL = {"bullish": "▲ bullish", "bearish": "▼ bearish", "neutral": "● neutral"}


def _render_card(r: dict, root_path: str = "") -> str:
    a          = r.get("analysis", {})
    importance = max(1, min(5, int(a.get("importance", 1))))
    sentiment  = a.get("sentiment", "neutral")
    tickers    = a.get("tickers", [])
    topics     = a.get("topics", [])
    key_points = a.get("key_points", [])
    pub_date   = format_pub_datetime(r.get("published", ""))

    sent_label  = _SENT_LABEL.get(sentiment, sentiment)
    vid_m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", r.get("url", ""))
    if vid_m:
        thumb_url  = f"https://img.youtube.com/vi/{vid_m.group(1)}/mqdefault.jpg"
        thumb_html = f'<div class="card-thumb"><img src="{thumb_url}" alt="" loading="lazy" onerror="this.closest(\'.card-thumb\').style.display=\'none\'"></div>'
    else:
        thumb_html = ""
    ticker_html = "".join(
        f'<a class="ticker" href="{root_path}ticker/{t}.html">{t}</a>'
        for t in tickers
    )
    topic_html  = "".join(f'<span class="topic">{t}</span>'  for t in topics)
    tags_block  = f'<div class="tags">{ticker_html}{topic_html}</div>' if (tickers or topics) else ""

    if key_points:
        items = "".join(f"<li>{p}</li>" for p in key_points)
        points_block = (
            f'<details class="points-details">'
            f'<summary>key points <span class="points-count">{len(key_points)}</span></summary>'
            f'<ul class="points">{items}</ul>'
            f'</details>'
        )
    else:
        points_block = ""

    return f"""<article class="card sent-{sentiment}" data-sentiment="{sentiment}">
  {thumb_html}
  <div class="card-top">
    <span class="channel-name">{r['channel_name']}</span>
    <span class="badge-sent {sentiment}">{sent_label}</span>
  </div>
  <h3><a href="{r['url']}" target="_blank" rel="noopener">{r['title']}</a></h3>
  {tags_block}
  <p class="summary">{a.get('summary_ja', '')}</p>
  {points_block}
  <div class="card-footer">
    <time>{pub_date}</time>
    <a class="watch-btn" href="{r['url']}" target="_blank" rel="noopener">watch ↗</a>
  </div>
</article>"""


def _bg_canvas_js(tickers: list[str]) -> str:
    """背景キャンバスアニメーション JS（ティッカー文字グリッド波紋版）を生成する。
    tickers が空の場合は 'ALPHADIGEST' をフォールバックとして使用する。
    """
    clean = list(dict.fromkeys(t.upper().strip()[:4] for t in tickers if t.strip()))
    tickers_json = json.dumps(clean if clean else [])
    js = r"""(function () {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const canvas = document.getElementById('bgCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const TICKERS = __TICKERS__;
  const chars = (TICKERS.length ? TICKERS.join(' ') : 'ALPHADIGEST').split('');

  // 4x4 Bayer ordered dither
  const BAYER = [[0,8,2,10],[12,4,14,6],[3,11,1,9],[15,7,13,5]];
  function dither(x, y) { return (BAYER[y & 3][x & 3] / 16) - 0.5; }

  // 1/f pink noise
  function pinkNoise() {
    const b = [0,0,0,0,0,0,0];
    return { next() {
      const w = (Math.random() - 0.5) * 2;
      b[0]=0.99886*b[0]+w*0.0555; b[1]=0.99332*b[1]+w*0.0751;
      b[2]=0.96900*b[2]+w*0.1539; b[3]=0.86650*b[3]+w*0.3105;
      b[4]=0.55000*b[4]+w*0.5330; b[5]=-0.7616*b[5]-w*0.0169;
      const v = b[0]+b[1]+b[2]+b[3]+b[4]+b[5]+b[6]+w*0.5362;
      b[6] = w * 0.1159;
      return v * 0.11;
    }};
  }

  const G = 13;
  const noise = pinkNoise();
  let W = 0, H = 0, phase = 0, last = performance.now();

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = W; canvas.height = H;
  }
  window.addEventListener('resize', resize, { passive: true });
  resize();

  function frame(ts) {
    const dt = Math.min(0.05, (ts - last) / 1000);
    last = ts;
    phase += (Math.PI * 2 / 10) * dt;

    const fv = noise.next();
    const ampScale = 1 + fv * 0.18;
    const intOff   = fv * 0.045;

    ctx.clearRect(0, 0, W, H);
    ctx.font = "bold 9px 'JetBrains Mono',monospace";
    ctx.textBaseline = 'top';

    const cols = Math.ceil(W / G);
    const rows = Math.ceil(H / G);
    const cy   = rows * 0.58;
    const base = rows / 4;
    const amp  = base * ampScale;
    const freq = 0.036;
    const sp2  = phase * 0.86;

    for (let y = 0; y < rows; y++) {
      for (let x = 0; x < cols; x++) {
        const w1   = Math.sin(x * freq + phase) * amp;
        const w2   = Math.cos(x * freq * 0.5 - sp2) * (base * 0.42 * ampScale);
        const dist = Math.abs(y - (cy + w1 + w2));
        let   int  = Math.max(0, 1 - dist / 12);
        int += Math.sin((x * 0.31 + y * 0.17) + phase * 0.35) * 0.035 + intOff;

        if (int + dither(x, y) > 0.5) {
          ctx.fillStyle = 'rgba(0,255,136,0.22)';
          ctx.fillText(chars[x % chars.length], x * G, y * G);
        }
      }
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();"""
    return js.replace('__TICKERS__', tickers_json)


def generate_html(results: list[dict], date_str: str, is_archive: bool = False, exec_summary: dict | None = None) -> str:
    results_sorted = sorted(
        results,
        key=lambda x: int(x.get("analysis", {}).get("importance", 1)),
        reverse=True,
    )

    # 背景アニメーション用ティッカー収集（重複排除・最大4文字）
    all_tickers = list(dict.fromkeys(
        t.upper().strip()[:4]
        for r in results
        for t in r.get("analysis", {}).get("tickers", [])
        if t.strip()
    ))

    n_bullish = sum(1 for r in results if r.get("analysis", {}).get("sentiment") == "bullish")
    n_bearish = sum(1 for r in results if r.get("analysis", {}).get("sentiment") == "bearish")
    n_neutral = len(results) - n_bullish - n_bearish
    now_jst   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    root_path    = "../" if is_archive else ""
    favicon_href = "../favicon.svg" if is_archive else "favicon.svg"
    nav_links = f"<a href='{root_path}archive/index.html' class='nav-link'>archive</a>"

    cards = "\n".join(_render_card(r, root_path) for r in results_sorted)
    if not cards:
        cards = '<div class="empty"><p>// 新着動画はありません</p></div>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>alphadigest — {date_str}</title>
  <link rel="icon" type="image/svg+xml" href="{favicon_href}">
  <meta property="og:title" content="alphadigest">
  <meta property="og:description" content="top investment voices, distilled.">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:      #08080c;
      --surface: #0c0c10;
      --border:  #1a1a1e;
      --brand:   #00ff88;
      --text:    #ffffff;
      --muted:   rgba(255,255,255,0.5);
      --dim:     rgba(255,255,255,0.3);
      --red:     #ff4444;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html {{ scroll-behavior: smooth; background: var(--bg); }}
    body {{
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      background: transparent; color: var(--text); min-height: 100vh; font-size: 14px;
    }}
    ::-webkit-scrollbar {{ width: 4px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: var(--border); }}

    /* header */
    header {{
      position: sticky; top: 0; z-index: 100;
      background: rgba(8,8,12,0.92); backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      padding: 0 clamp(16px,4vw,48px);
    }}
    .header-inner {{
      max-width: 1440px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between;
      height: 52px; gap: 31px;
    }}
    .header-ticker {{
      flex: 1; min-width: 0; height: 46px;
      position: relative; overflow: hidden;
    }}
    .header-ticker::before,
    .header-ticker::after {{
      content: ''; position: absolute; top: 0; bottom: 0; width: 48px; z-index: 2; pointer-events: none;
    }}
    .header-ticker::before {{
      left: 0;
      background: linear-gradient(to right, rgba(8,8,12,0.92), transparent);
    }}
    .header-ticker::after {{
      right: 0;
      background: linear-gradient(to left, rgba(8,8,12,0.92), transparent);
    }}
    .header-ticker .tradingview-widget-container,
    .header-ticker .tradingview-widget-container__widget {{ height: 46px !important; }}
    .ad-logo {{
      display: inline-flex; align-items: center; font-size: 18px;
      line-height: 1; letter-spacing: -0.5px; text-decoration: none; flex-shrink: 0;
    }}
    .ad-logo__cursor {{
      display: inline-block; width: 2px; height: 1.1em;
      background: var(--brand); opacity: 0.8; border-radius: 1px; margin-right: 7px;
      animation: blink 1.2s step-end infinite;
    }}
    .ad-logo__alpha {{ color: var(--brand); font-weight: 700; }}
    .ad-logo__digest {{ color: var(--text); font-weight: 400; opacity: 0.5; }}
    @keyframes blink {{ 50% {{ opacity: 0; }} }}
    nav {{ display: flex; align-items: center; gap: 4px; flex-shrink: 0; }}
    .nav-link {{
      font-size: 0.7rem; color: var(--muted); text-decoration: none;
      padding: 4px 11px; border: 1px solid var(--border); border-radius: 2px;
      font-family: inherit; transition: all 150ms ease;
    }}
    .nav-link:hover {{ color: var(--brand); border-color: rgba(0,255,136,0.3); }}

    /* executive summary / hero */
    .exec-summary {{
      max-width: 1440px; margin: 0 auto;
      padding: clamp(24px,4vw,44px) clamp(16px,4vw,48px) clamp(16px,3vw,28px);
    }}
    .exec-inner {{
      border-left: 3px solid var(--exec-accent, rgba(255,255,255,0.15));
      padding-left: 18px;
      transition: transform 80ms ease;
    }}
    .exec-inner:hover {{ transform: translateY(-2px); }}
    .exec-header {{
      display: flex; align-items: center; gap: 20px; flex-wrap: wrap; margin-bottom: 14px;
    }}
    .exec-label {{
      font-size: 0.82rem; font-weight: 700; color: var(--brand); letter-spacing: 1px; flex-shrink: 0;
    }}
    .exec-stats-row {{
      display: flex; align-items: center; gap: 14px; flex-wrap: wrap; font-size: 0.7rem;
    }}
    .exec-date {{ color: var(--brand); font-weight: 700; border: 1px solid rgba(0,255,136,0.25); padding: 1px 8px; border-radius: 2px; }}
    .exec-stat-total {{ color: var(--muted); }}
    .exec-stat-bull  {{ color: var(--brand); }}
    .exec-stat-bear  {{ color: var(--red); }}
    .exec-stat-neut  {{ color: var(--muted); }}
    .exec-grid {{
      display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px;
    }}
    .exec-block {{
      background: var(--surface); border: 1px solid var(--border); border-radius: 2px; padding: 14px 16px;
    }}
    .exec-block-label {{
      font-size: 0.58rem; color: var(--brand); letter-spacing: 2px; margin-bottom: 10px;
    }}
    .exec-ticker-item {{
      padding-bottom: 10px; margin-bottom: 10px; border-bottom: 1px solid var(--border);
    }}
    .exec-ticker-item:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
    .exec-ticker-item .ticker {{ margin-bottom: 4px; display: inline-block; }}
    .exec-ticker-title {{ font-size: 0.78rem; line-height: 1.5; margin: 3px 0 1px; }}
    .exec-ticker-title a {{ color: rgba(255,255,255,0.82); text-decoration: none; }}
    .exec-ticker-title a:hover {{ color: #fff; text-decoration: underline; }}
    .exec-ticker-ch  {{ font-size: 0.63rem; color: var(--muted); }}
    .exec-empty {{ font-size: 0.72rem; color: var(--dim); }}
    /* archive page: minimal hero */
    .hero {{
      max-width: 1440px; margin: 0 auto;
      padding: clamp(24px,4vw,44px) clamp(16px,4vw,48px) clamp(16px,3vw,28px);
    }}
    .hero-prompt {{
      font-size: 0.72rem; color: var(--muted);
      display: flex; align-items: center; gap: 8px; margin-bottom: 16px;
    }}
    .prompt-symbol {{ color: var(--brand); opacity: 0.7; }}
    .prompt-date {{
      color: var(--brand); font-weight: 700;
      border: 1px solid rgba(0,255,136,0.25); padding: 2px 10px;
      border-radius: 2px; font-size: 0.82rem;
    }}
    .hero-stats {{
      display: flex; gap: clamp(16px,3vw,40px); flex-wrap: wrap; align-items: baseline;
    }}
    .h-stat {{ text-align: center; }}
    .h-stat-num {{ font-size: clamp(1.4rem,3vw,1.9rem); font-weight: 700; line-height: 1; }}
    .h-stat-num.c-total {{ color: var(--text); }}
    .h-stat-num.c-bull  {{ color: var(--brand); }}
    .h-stat-num.c-bear  {{ color: var(--red); }}
    .h-stat-num.c-neut  {{ color: var(--muted); }}
    .h-stat-label {{ font-size: 0.6rem; color: var(--dim); margin-top: 5px; letter-spacing: 0.1em; }}
    @media (max-width: 640px) {{ .exec-grid {{ grid-template-columns: 1fr; }} }}

    /* filters */
    .filters {{
      max-width: 1440px; margin: 0 auto 20px;
      padding: 0 clamp(16px,4vw,48px);
      display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    }}
    .filter-segment {{
      display: flex; background: var(--surface);
      border: 1px solid var(--border); border-radius: 2px; padding: 2px; gap: 2px;
    }}
    .filter-btn {{
      background: transparent; border: none; color: var(--muted);
      border-radius: 2px; padding: 5px 14px;
      font-size: 0.7rem; font-family: inherit; cursor: pointer;
      transition: all 150ms ease; white-space: nowrap;
    }}
    .filter-btn:hover {{ color: var(--text); }}
    .filter-btn.active {{ color: var(--bg); background: var(--text); }}
    .filter-btn.f-bull.active {{ color: var(--bg); background: var(--brand); }}
    .filter-btn.f-bear.active {{ color: var(--text); background: var(--red); }}
    .filter-btn.f-neut.active {{ color: var(--bg); background: rgba(255,255,255,0.5); }}

    /* grid */
    .grid-wrap {{ max-width: 1440px; margin: 0 auto; padding: 0 clamp(16px,4vw,48px) 80px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
      gap: 10px;
    }}
    #no-results {{ display: none; text-align: center; color: var(--dim); padding: 80px 20px; grid-column: 1/-1; font-size: 0.78rem; }}
    .empty {{ text-align: center; color: var(--dim); padding: 80px 20px; font-size: 0.78rem; }}

    /* cards */
    .card {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 2px; padding: 18px 20px; position: relative;
      opacity: 0; transform: translateY(14px);
      transition: opacity 350ms ease, transform 350ms ease, border-color 150ms ease;
    }}
    .card.visible {{ opacity: 1; transform: translateY(0); }}
    .card.hidden  {{ display: none !important; }}
    .card:hover   {{ transform: translateY(-2px); transition: opacity 350ms ease, transform 80ms ease, border-color 150ms ease; }}
    .card.sent-bullish {{ border-left: 2px solid var(--brand); }}
    .card.sent-bearish {{ border-left: 2px solid var(--red); }}
    .card.sent-neutral {{ border-left: 2px solid rgba(255,255,255,0.12); }}
    .card.sent-bullish:hover {{ border-color: rgba(0,255,136,0.25); border-left-color: var(--brand); }}
    .card.sent-bearish:hover {{ border-color: rgba(255,68,68,0.25); border-left-color: var(--red); }}

    .card-thumb {{ margin: -18px -20px 14px; border-radius: 1px 1px 0 0; overflow: hidden; line-height: 0; }}
    .card-thumb img {{ width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }}
    .card-top {{
      display: flex; justify-content: space-between; align-items: center;
      gap: 8px; margin-bottom: 8px;
    }}
    .channel-name {{
      font-size: 0.63rem; font-weight: 500; color: var(--brand);
      opacity: 0.65; letter-spacing: 0.03em;
    }}
    .channel-name::before {{ content: '// '; opacity: 0.7; }}
    .badge-sent {{
      font-size: 0.6rem; font-weight: 700; letter-spacing: 0.05em;
      padding: 2px 8px; border-radius: 2px; border: 1px solid; flex-shrink: 0;
    }}
    .badge-sent.bullish {{ color: var(--brand); border-color: rgba(0,255,136,0.3); }}
    .badge-sent.bearish {{ color: var(--red);   border-color: rgba(255,68,68,0.3); }}
    .badge-sent.neutral {{ color: var(--muted); border-color: var(--border); }}

    h3 {{ font-size: 0.87rem; font-weight: 700; line-height: 1.55; margin-bottom: 10px; }}
    h3 a {{ color: var(--text); text-decoration: none; }}
    h3 a:hover {{ color: var(--brand); }}

    .tags {{ display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }}
    .ticker {{
      font-size: 0.65rem; font-weight: 700; color: var(--brand);
      border: 1px solid rgba(0,255,136,0.25); padding: 1px 7px; border-radius: 2px;
    }}
    .topic {{
      font-size: 0.63rem; color: var(--muted);
      border: 1px solid var(--border); padding: 1px 7px; border-radius: 2px;
    }}

    .summary {{ font-size: 0.81rem; line-height: 1.82; color: rgba(255,255,255,0.75); margin-bottom: 10px; }}
    .summary b {{ color: var(--text); font-weight: 700; }}

    .points-details {{ margin-bottom: 10px; }}
    .points-details > summary {{
      font-size: 0.68rem; color: var(--dim); cursor: pointer;
      display: flex; align-items: center; gap: 6px;
      padding: 3px 0; list-style: none; user-select: none; transition: color 150ms;
    }}
    .points-details > summary::-webkit-details-marker {{ display: none; }}
    .points-details > summary::before {{
      content: '›'; font-size: 1rem; display: inline-block; transition: transform 150ms ease;
    }}
    .points-details[open] > summary::before {{ transform: rotate(90deg); }}
    .points-details > summary:hover {{ color: var(--muted); }}
    .points-count {{
      background: var(--border); padding: 0 5px; font-size: 0.6rem; border-radius: 2px;
    }}
    .points {{ list-style: none; padding: 6px 0 0; }}
    .points li {{
      font-size: 0.77rem; color: var(--muted); line-height: 1.75;
      padding-left: 16px; position: relative; margin-bottom: 2px;
    }}
    .points li::before {{ content: '>'; position: absolute; left: 0; color: var(--dim); }}

    .card-footer {{
      display: flex; justify-content: space-between; align-items: center;
      padding-top: 10px; border-top: 1px solid var(--border); margin-top: 8px;
    }}
    .card-footer time {{ font-size: 0.62rem; color: var(--dim); }}
    .watch-btn {{
      font-size: 0.66rem; font-weight: 700; color: var(--muted);
      text-decoration: none; padding: 4px 12px;
      border: 1px solid var(--border); border-radius: 2px;
      transition: all 150ms ease; font-family: inherit;
    }}
    .watch-btn:hover {{ color: var(--bg); background: var(--brand); border-color: var(--brand); }}

    footer {{
      text-align: center; padding: 20px 16px;
      border-top: 1px solid var(--border); font-size: 0.62rem; color: var(--dim);
    }}

    @media (max-width: 640px) {{
      .header-ticker {{ display: none; }}
      .hero h1 {{ font-size: 1.7rem; }}
      .grid {{ grid-template-columns: 1fr; }}
      .filters {{ flex-wrap: nowrap; overflow-x: auto; padding-bottom: 6px; }}
      .filters::-webkit-scrollbar {{ display: none; }}
    }}
    /* bg canvas */
    #bgCanvas {{
      position: fixed; inset: 0; width: 100%; height: 100%;
      z-index: -1; pointer-events: none;
    }}
  </style>
</head>
<body>
  <canvas id="bgCanvas" aria-hidden="true"></canvas>

  <header>
    <div class="header-inner">
      <a href="{root_path}index.html" class="ad-logo">
        <span class="ad-logo__cursor"></span>
        <span class="ad-logo__alpha">alpha</span>
        <span class="ad-logo__digest">digest</span>
      </a>
      <div class="header-ticker">
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js" async>
          {{
            "symbols": [
              {{"proName": "FOREXCOM:SPXUSD", "title": "S&P 500"}},
              {{"proName": "FOREXCOM:NSXUSD", "title": "Nasdaq 100"}},
              {{"description": "Dow Jones", "proName": "FOREXCOM:DJI"}},
              {{"description": "VIX", "proName": "CBOE:VIX"}},
              {{"description": "USD/JPY", "proName": "FX_IDC:USDJPY"}},
              {{"description": "Gold", "proName": "TVC:GOLD"}},
              {{"description": "Bitcoin", "proName": "BITSTAMP:BTCUSD"}}
            ],
            "showSymbolLogo": false,
            "isTransparent": true,
            "displayMode": "adaptive",
            "colorTheme": "dark",
            "locale": "en"
          }}
          </script>
        </div>
      </div>
      <nav>
        <a href="{root_path}channels.html" class="nav-link">channels</a>
        {nav_links}
      </nav>
    </div>
  </header>

  {_render_hero(date_str, len(results), n_bullish, n_bearish, n_neutral, exec_summary)}

  <div class="filters">
    <div class="filter-segment">
      <button class="filter-btn active" data-filter="all">all</button>
      <button class="filter-btn f-bull" data-filter="bullish">▲ bullish</button>
      <button class="filter-btn f-bear" data-filter="bearish">▼ bearish</button>
      <button class="filter-btn f-neut" data-filter="neutral">● neutral</button>
    </div>
  </div>

  <div class="grid-wrap">
    <div class="grid" id="grid">
      {cards}
      <div id="no-results">// no results matching filters</div>
    </div>
  </div>

  <footer>// top investment voices, distilled &nbsp;·&nbsp; {now_jst} &nbsp;·&nbsp; powered by <a href="https://suno.com/@crazyzenmonk" target="_blank" rel="noopener" style="color:inherit;text-decoration:none;border-bottom:1px solid rgba(255,255,255,0.2);">CZM PROJECT</a></footer>

  <script>
    // IntersectionObserver card fade-in
    const observer = new IntersectionObserver((entries) => {{
      entries.forEach(entry => {{
        if (entry.isIntersecting) {{
          entry.target.classList.add('visible');
          observer.unobserve(entry.target);
        }}
      }});
    }}, {{ threshold: 0.06 }});
    document.querySelectorAll('.card').forEach((c, i) => {{
      c.style.transitionDelay = Math.min(i * 35, 280) + 'ms';
      observer.observe(c);
    }});

    // Filters
    let activeSentiment = 'all';
    const allCards = document.querySelectorAll('.card');

    function applyFilters() {{
      let visible = 0;
      allCards.forEach(c => {{
        const matchS = activeSentiment === 'all' || c.dataset.sentiment === activeSentiment;
        if (matchS) {{ c.classList.remove('hidden'); visible++; }}
        else {{ c.classList.add('hidden'); }}
      }});
      document.getElementById('no-results').style.display = visible === 0 ? 'block' : 'none';
    }}

    document.querySelectorAll('[data-filter]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        btn.closest('.filter-segment').querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        activeSentiment = btn.dataset.filter;
        applyFilters();
      }});
    }});

    // bg canvas
    {_bg_canvas_js(all_tickers)}
  </script>

</body>
</html>"""


# ---- アーカイブ一覧ページ -----------------------------------------------

def generate_archive_index_html(dates: list[str]) -> str:
    """過去の日次ダイジェスト一覧ページを生成する。"""
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    rows = "".join(
        f'<a class="archive-row" href="{d}.html">'
        f'<span class="arc-idx">{str(i+1).zfill(2)}</span>'
        f'<span class="arc-date">{d}</span>'
        f'<span class="arc-arrow">→</span>'
        f'</a>'
        for i, d in enumerate(dates)
    )
    if not rows:
        rows = '<p class="arc-empty">// アーカイブはまだありません</p>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>archive — alphadigest</title>
  <link rel="icon" type="image/svg+xml" href="../favicon.svg">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #08080c; --surface: #0c0c10; --border: #1a1a1e;
      --brand: #00ff88; --text: #ffffff;
      --muted: rgba(255,255,255,0.5); --dim: rgba(255,255,255,0.3);
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html {{ background: var(--bg); }}
    body {{
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      background: transparent; color: var(--text); min-height: 100vh; font-size: 14px;
    }}
    ::-webkit-scrollbar {{ width: 4px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: var(--border); }}
    header {{
      position: sticky; top: 0; z-index: 100;
      background: rgba(8,8,12,0.92); backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border); padding: 0 clamp(16px,4vw,48px);
    }}
    .header-inner {{
      max-width: 1440px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between;
      height: 52px; gap: 31px;
    }}
    .header-ticker {{
      flex: 1; min-width: 0; height: 46px;
      position: relative; overflow: hidden;
    }}
    .header-ticker::before,
    .header-ticker::after {{
      content: ''; position: absolute; top: 0; bottom: 0; width: 48px; z-index: 2; pointer-events: none;
    }}
    .header-ticker::before {{
      left: 0;
      background: linear-gradient(to right, rgba(8,8,12,0.92), transparent);
    }}
    .header-ticker::after {{
      right: 0;
      background: linear-gradient(to left, rgba(8,8,12,0.92), transparent);
    }}
    .header-ticker .tradingview-widget-container,
    .header-ticker .tradingview-widget-container__widget {{ height: 46px !important; }}
    .ad-logo {{
      display: inline-flex; align-items: center; font-size: 18px;
      line-height: 1; letter-spacing: -0.5px; text-decoration: none; flex-shrink: 0;
    }}
    .ad-logo__cursor {{
      display: inline-block; width: 2px; height: 1.1em;
      background: var(--brand); opacity: 0.8; border-radius: 1px; margin-right: 7px;
      animation: blink 1.2s step-end infinite;
    }}
    .ad-logo__alpha {{ color: var(--brand); font-weight: 700; }}
    .ad-logo__digest {{ color: var(--text); font-weight: 400; opacity: 0.5; }}
    @keyframes blink {{ 50% {{ opacity: 0; }} }}
    nav {{ display: flex; align-items: center; gap: 4px; flex-shrink: 0; }}
    .nav-link {{
      font-size: 0.7rem; color: var(--muted); text-decoration: none;
      padding: 4px 11px; border: 1px solid var(--border); border-radius: 2px;
      font-family: inherit; transition: all 150ms ease;
    }}
    .nav-link:hover {{ color: var(--brand); border-color: rgba(0,255,136,0.3); }}
    main {{ max-width: 720px; margin: 0 auto; padding: clamp(40px,6vw,72px) clamp(16px,4vw,48px) 80px; }}
    .page-prompt {{
      font-size: 0.72rem; color: var(--muted);
      display: flex; align-items: center; gap: 8px; margin-bottom: 20px;
    }}
    .prompt-symbol {{ color: var(--brand); opacity: 0.7; }}
    h1 {{ font-size: clamp(1.6rem,4vw,2rem); font-weight: 700; letter-spacing: -0.03em; margin-bottom: 8px; }}
    h1 .green {{ color: var(--brand); }}
    .subtitle {{ color: var(--dim); font-size: 0.72rem; margin-bottom: 32px; }}
    .archive-list {{ display: flex; flex-direction: column; gap: 4px; }}
    .archive-row {{
      display: flex; align-items: center; gap: 16px;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 2px; padding: 12px 16px; text-decoration: none;
      transition: all 150ms ease;
    }}
    .archive-row:hover {{ border-color: rgba(0,255,136,0.25); transform: translateX(4px); }}
    .arc-idx {{ font-size: 0.6rem; color: var(--dim); min-width: 22px; }}
    .arc-date {{ font-size: 0.9rem; font-weight: 700; color: var(--text); flex: 1; }}
    .arc-arrow {{ color: var(--brand); font-size: 0.85rem; opacity: 0.7; }}
    .arc-empty {{ color: var(--dim); text-align: center; padding: 40px; font-size: 0.78rem; }}
    footer {{ text-align: center; padding: 20px; border-top: 1px solid var(--border); font-size: 0.62rem; color: var(--dim); }}
    #bgCanvas {{ position: fixed; inset: 0; width: 100%; height: 100%; z-index: -1; pointer-events: none; }}
  </style>
</head>
<body>
  <canvas id="bgCanvas" aria-hidden="true"></canvas>
  <header>
    <div class="header-inner">
      <a href="../index.html" class="ad-logo">
        <span class="ad-logo__cursor"></span>
        <span class="ad-logo__alpha">alpha</span>
        <span class="ad-logo__digest">digest</span>
      </a>
      <div class="header-ticker">
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js" async>
          {{
            "symbols": [
              {{"proName": "FOREXCOM:SPXUSD", "title": "S&P 500"}},
              {{"proName": "FOREXCOM:NSXUSD", "title": "Nasdaq 100"}},
              {{"description": "Dow Jones", "proName": "FOREXCOM:DJI"}},
              {{"description": "VIX", "proName": "CBOE:VIX"}},
              {{"description": "USD/JPY", "proName": "FX_IDC:USDJPY"}},
              {{"description": "Gold", "proName": "TVC:GOLD"}},
              {{"description": "Bitcoin", "proName": "BITSTAMP:BTCUSD"}}
            ],
            "showSymbolLogo": false,
            "isTransparent": true,
            "displayMode": "adaptive",
            "colorTheme": "dark",
            "locale": "en"
          }}
          </script>
        </div>
      </div>
      <nav>
        <a href="../channels.html" class="nav-link">channels</a>
        <a href="index.html" class="nav-link">archive</a>
      </nav>
    </div>
  </header>
  <main>
    <div class="page-prompt">
      <span class="prompt-symbol">></span>
      <span>archive</span>
      <span style="color:var(--dim)">{len(dates)} digests</span>
    </div>
    <h1><span class="green">alpha</span>digest archive</h1>
    <p class="subtitle">// 過去の投資YouTube日次ダイジェスト一覧</p>
    <div class="archive-list">{rows}</div>
  </main>
  <footer>// top investment voices, distilled &nbsp;·&nbsp; {now_jst} &nbsp;·&nbsp; powered by <a href="https://suno.com/@crazyzenmonk" target="_blank" rel="noopener" style="color:inherit;text-decoration:none;border-bottom:1px solid rgba(255,255,255,0.2);">CZM PROJECT</a></footer>
  <script>{_bg_canvas_js([])}</script>
</body>
</html>"""


# ---- チャンネル一覧ページ -----------------------------------------------

def generate_channels_html(channels: list[dict]) -> str:
    """監視対象チャンネル一覧ページを生成する。"""
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    rows = "".join(
        f'<a class="channel-row" href="https://youtube.com/@{ch["handle"]}" target="_blank" rel="noopener">'
        f'<span class="ch-idx">{str(i+1).zfill(2)}</span>'
        f'<span class="ch-name">{ch["name"]}</span>'
        f'<span class="ch-handle">@{ch["handle"]}</span>'
        f'<span class="ch-arrow">↗</span>'
        f'</a>'
        for i, ch in enumerate(channels)
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>channels — alphadigest</title>
  <link rel="icon" type="image/svg+xml" href="favicon.svg">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #08080c; --surface: #0c0c10; --border: #1a1a1e;
      --brand: #00ff88; --text: #ffffff;
      --muted: rgba(255,255,255,0.5); --dim: rgba(255,255,255,0.3);
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html {{ background: var(--bg); }}
    body {{
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      background: transparent; color: var(--text); min-height: 100vh; font-size: 14px;
    }}
    ::-webkit-scrollbar {{ width: 4px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: var(--border); }}
    header {{
      position: sticky; top: 0; z-index: 100;
      background: rgba(8,8,12,0.92); backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border); padding: 0 clamp(16px,4vw,48px);
    }}
    .header-inner {{
      max-width: 1440px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between;
      height: 52px; gap: 31px;
    }}
    .header-ticker {{
      flex: 1; min-width: 0; height: 46px;
      position: relative; overflow: hidden;
    }}
    .header-ticker::before,
    .header-ticker::after {{
      content: ''; position: absolute; top: 0; bottom: 0; width: 48px; z-index: 2; pointer-events: none;
    }}
    .header-ticker::before {{
      left: 0;
      background: linear-gradient(to right, rgba(8,8,12,0.92), transparent);
    }}
    .header-ticker::after {{
      right: 0;
      background: linear-gradient(to left, rgba(8,8,12,0.92), transparent);
    }}
    .header-ticker .tradingview-widget-container,
    .header-ticker .tradingview-widget-container__widget {{ height: 46px !important; }}
    .ad-logo {{
      display: inline-flex; align-items: center; font-size: 18px;
      line-height: 1; letter-spacing: -0.5px; text-decoration: none; flex-shrink: 0;
    }}
    .ad-logo__cursor {{
      display: inline-block; width: 2px; height: 1.1em;
      background: var(--brand); opacity: 0.8; border-radius: 1px; margin-right: 7px;
      animation: blink 1.2s step-end infinite;
    }}
    .ad-logo__alpha {{ color: var(--brand); font-weight: 700; }}
    .ad-logo__digest {{ color: var(--text); font-weight: 400; opacity: 0.5; }}
    @keyframes blink {{ 50% {{ opacity: 0; }} }}
    nav {{ display: flex; align-items: center; gap: 4px; flex-shrink: 0; }}
    .nav-link {{
      font-size: 0.7rem; color: var(--muted); text-decoration: none;
      padding: 4px 11px; border: 1px solid var(--border); border-radius: 2px;
      font-family: inherit; transition: all 150ms ease;
    }}
    .nav-link:hover {{ color: var(--brand); border-color: rgba(0,255,136,0.3); }}
    main {{ max-width: 720px; margin: 0 auto; padding: clamp(40px,6vw,72px) clamp(16px,4vw,48px) 80px; }}
    .page-prompt {{
      font-size: 0.72rem; color: var(--muted);
      display: flex; align-items: center; gap: 8px; margin-bottom: 20px;
    }}
    .prompt-symbol {{ color: var(--brand); opacity: 0.7; }}
    h1 {{ font-size: clamp(1.6rem,4vw,2rem); font-weight: 700; letter-spacing: -0.03em; margin-bottom: 8px; }}
    h1 .green {{ color: var(--brand); }}
    .subtitle {{ color: var(--dim); font-size: 0.72rem; margin-bottom: 32px; }}
    .channel-list {{ display: flex; flex-direction: column; gap: 4px; }}
    .channel-row {{
      display: flex; align-items: center; gap: 16px;
      background: var(--surface); border: 1px solid var(--border);
      border-left: 2px solid rgba(0,255,136,0.12);
      border-radius: 2px; padding: 12px 16px; text-decoration: none;
      transition: all 150ms ease;
    }}
    .channel-row:hover {{
      border-color: rgba(0,255,136,0.2); border-left-color: var(--brand);
      transform: translateX(4px);
    }}
    .ch-idx {{ font-size: 0.6rem; color: var(--dim); min-width: 20px; flex-shrink: 0; }}
    .ch-name {{ font-size: 0.88rem; font-weight: 700; color: var(--text); flex: 1; }}
    .ch-handle {{ font-size: 0.67rem; color: var(--muted); flex-shrink: 0; }}
    .ch-arrow {{ color: var(--brand); font-size: 0.78rem; opacity: 0.6; flex-shrink: 0; }}
    @media (max-width: 480px) {{
      .ch-handle {{ display: none; }}
    }}
    footer {{ text-align: center; padding: 20px; border-top: 1px solid var(--border); font-size: 0.62rem; color: var(--dim); }}
    #bgCanvas {{ position: fixed; inset: 0; width: 100%; height: 100%; z-index: -1; pointer-events: none; }}
  </style>
</head>
<body>
  <canvas id="bgCanvas" aria-hidden="true"></canvas>
  <header>
    <div class="header-inner">
      <a href="index.html" class="ad-logo">
        <span class="ad-logo__cursor"></span>
        <span class="ad-logo__alpha">alpha</span>
        <span class="ad-logo__digest">digest</span>
      </a>
      <div class="header-ticker">
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js" async>
          {{
            "symbols": [
              {{"proName": "FOREXCOM:SPXUSD", "title": "S&P 500"}},
              {{"proName": "FOREXCOM:NSXUSD", "title": "Nasdaq 100"}},
              {{"description": "Dow Jones", "proName": "FOREXCOM:DJI"}},
              {{"description": "VIX", "proName": "CBOE:VIX"}},
              {{"description": "USD/JPY", "proName": "FX_IDC:USDJPY"}},
              {{"description": "Gold", "proName": "TVC:GOLD"}},
              {{"description": "Bitcoin", "proName": "BITSTAMP:BTCUSD"}}
            ],
            "showSymbolLogo": false,
            "isTransparent": true,
            "displayMode": "adaptive",
            "colorTheme": "dark",
            "locale": "en"
          }}
          </script>
        </div>
      </div>
      <nav>
        <a href="channels.html" class="nav-link">channels</a>
        <a href="archive/index.html" class="nav-link">archive</a>
      </nav>
    </div>
  </header>
  <main>
    <div class="page-prompt">
      <span class="prompt-symbol">></span>
      <span>channels</span>
      <span style="color:var(--dim)">{len(channels)} tracked</span>
    </div>
    <h1><span class="green">tracked</span> channels</h1>
    <p class="subtitle">// 監視対象の米国投資系YouTubeチャンネル一覧</p>
    <div class="channel-list">{rows}</div>
  </main>
  <footer>// top investment voices, distilled &nbsp;·&nbsp; {now_jst} &nbsp;·&nbsp; powered by <a href="https://suno.com/@crazyzenmonk" target="_blank" rel="noopener" style="color:inherit;text-decoration:none;border-bottom:1px solid rgba(255,255,255,0.2);">CZM PROJECT</a></footer>
  <script>{_bg_canvas_js([])}</script>
</body>
</html>"""


# ---- ティッカーページ ---------------------------------------------------

def _pct_html(price_on_date: float | None, current_price: float | None) -> str:
    """言及時株価→現在株価の騰落率HTMLを返す。"""
    if not price_on_date or not current_price:
        return '<span class="m-pct m-pct-na">—</span>'
    pct = (current_price - price_on_date) / price_on_date * 100
    sign = "+" if pct >= 0 else ""
    cls = "m-pct-up" if pct >= 0 else "m-pct-dn"
    return f'<span class="m-pct {cls}">{sign}{pct:.1f}%</span>'


def generate_ticker_page(ticker: str, mentions: list[dict], current_price: float | None = None) -> str:
    """ティッカーシンボル別ダッシュボードページを生成する。"""
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    mention_rows = "".join(
        f'<a class="mention-row" href="../{m["page_path"]}">'
        f'<span class="m-date">{m["date"]}</span>'
        f'<span class="m-channel">{m["channel_name"]}</span>'
        f'<span class="m-price">{"$%.2f" % m["price_on_date"] if m["price_on_date"] else "—"}</span>'
        f'{_pct_html(m["price_on_date"], current_price)}'
        f'<span class="m-title">{m["title"]}</span>'
        f'<span class="m-arrow">↗</span>'
        f'</a>'
        for m in mentions
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{ticker} — alphadigest</title>
  <link rel="icon" type="image/svg+xml" href="../favicon.svg">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #08080c; --surface: #0c0c10; --border: #1a1a1e;
      --brand: #00ff88; --text: #ffffff;
      --muted: rgba(255,255,255,0.5); --dim: rgba(255,255,255,0.3);
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html {{ background: var(--bg); }}
    body {{
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      background: transparent; color: var(--text); min-height: 100vh; font-size: 14px;
    }}
    ::-webkit-scrollbar {{ width: 4px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: var(--border); }}
    header {{
      position: sticky; top: 0; z-index: 100;
      background: rgba(8,8,12,0.92); backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border); padding: 0 clamp(16px,4vw,48px);
    }}
    .header-inner {{
      max-width: 960px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between; height: 52px;
    }}
    .ad-logo {{
      display: inline-flex; align-items: center; font-size: 18px;
      line-height: 1; letter-spacing: -0.5px; text-decoration: none;
    }}
    .ad-logo__cursor {{
      display: inline-block; width: 2px; height: 1.1em;
      background: var(--brand); opacity: 0.8; border-radius: 1px; margin-right: 7px;
      animation: blink 1.2s step-end infinite;
    }}
    .ad-logo__alpha {{ color: var(--brand); font-weight: 700; }}
    .ad-logo__digest {{ color: var(--text); font-weight: 400; opacity: 0.5; }}
    @keyframes blink {{ 50% {{ opacity: 0; }} }}
    nav {{ display: flex; gap: 4px; }}
    .nav-link {{
      font-size: 0.7rem; color: var(--muted); text-decoration: none;
      padding: 4px 11px; border: 1px solid var(--border); border-radius: 2px;
      font-family: inherit; transition: all 150ms ease;
    }}
    .nav-link:hover {{ color: var(--brand); border-color: rgba(0,255,136,0.3); }}
    main {{ max-width: 960px; margin: 0 auto; padding: clamp(32px,5vw,64px) clamp(16px,4vw,48px) 80px; }}
    .page-prompt {{
      font-size: 0.72rem; color: var(--muted);
      display: flex; align-items: center; gap: 8px; margin-bottom: 16px;
    }}
    .prompt-symbol {{ color: var(--brand); opacity: 0.7; }}
    .ticker-hero {{
      display: flex; align-items: baseline; gap: 16px; margin-bottom: 32px; flex-wrap: wrap;
    }}
    h1 {{
      font-size: clamp(2rem,5vw,3rem); font-weight: 700;
      letter-spacing: -0.04em; color: var(--brand);
    }}
    .mention-count {{
      font-size: 0.72rem; color: var(--dim);
      border: 1px solid var(--border); padding: 3px 10px; border-radius: 2px;
    }}
    /* TradingView widgets layout */
    .widgets-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      grid-template-rows: auto auto;
      gap: 10px;
      margin-bottom: 32px;
    }}
    .widget-box {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 2px; overflow: hidden;
    }}
    .widget-box.span2 {{ grid-column: 1 / -1; }}
    .widget-label {{
      font-size: 0.6rem; color: var(--dim); letter-spacing: 0.1em;
      padding: 8px 12px 4px; border-bottom: 1px solid var(--border);
    }}
    @media (max-width: 640px) {{
      .widgets-grid {{ grid-template-columns: 1fr; }}
      .widget-box.span2 {{ grid-column: 1; }}
    }}
    /* Mention history */
    .section-title {{
      font-size: 0.68rem; color: var(--dim); letter-spacing: 0.1em;
      margin-bottom: 10px;
    }}
    .mention-list {{ display: flex; flex-direction: column; gap: 4px; margin-bottom: 40px; }}
    .mention-row {{
      display: grid;
      grid-template-columns: 96px 148px 72px 60px 1fr 20px;
      align-items: center; gap: 10px;
      background: var(--surface); border: 1px solid var(--border);
      border-left: 2px solid rgba(0,255,136,0.15);
      border-radius: 2px; padding: 10px 14px;
      text-decoration: none; transition: all 150ms ease;
    }}
    .mention-row:hover {{ border-left-color: var(--brand); transform: translateX(3px); }}
    .m-date    {{ font-size: 0.65rem; color: var(--dim); flex-shrink: 0; }}
    .m-channel {{ font-size: 0.65rem; color: var(--brand); opacity: 0.7; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .m-price   {{ font-size: 0.72rem; color: var(--muted); font-weight: 700; text-align: right; }}
    .m-pct     {{ font-size: 0.72rem; font-weight: 700; text-align: right; }}
    .m-pct-up  {{ color: var(--brand); }}
    .m-pct-dn  {{ color: #ff4444; }}
    .m-pct-na  {{ color: var(--dim); }}
    .m-title   {{ font-size: 0.78rem; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .m-arrow   {{ color: var(--brand); font-size: 0.72rem; opacity: 0.6; }}
    .no-mentions {{ color: var(--dim); font-size: 0.78rem; padding: 20px 0; }}
    @media (max-width: 640px) {{
      .mention-row {{ grid-template-columns: 80px 60px 48px 1fr 16px; }}
      .m-channel {{ display: none; }}
    }}
    footer {{ text-align: center; padding: 20px; border-top: 1px solid var(--border); font-size: 0.62rem; color: var(--dim); }}
    #bgCanvas {{ position: fixed; inset: 0; width: 100%; height: 100%; z-index: -1; pointer-events: none; }}
  </style>
</head>
<body>
  <canvas id="bgCanvas" aria-hidden="true"></canvas>
  <header>
    <div class="header-inner">
      <a href="../index.html" class="ad-logo">
        <span class="ad-logo__cursor"></span>
        <span class="ad-logo__alpha">alpha</span>
        <span class="ad-logo__digest">digest</span>
      </a>
      <nav>
        <a href="../channels.html" class="nav-link">channels</a>
        <a href="../archive/index.html" class="nav-link">archive</a>
      </nav>
    </div>
  </header>
  <main>
    <div class="page-prompt">
      <span class="prompt-symbol">></span>
      <span>ticker</span>
      <span style="color:var(--brand);font-weight:700">{ticker}</span>
    </div>
    <div class="ticker-hero">
      <h1>{ticker}</h1>
      <span class="mention-count">{len(mentions)} mentions in archive</span>
    </div>

    <div class="widgets-grid">
      <!-- Symbol Info: 価格・時価総額・PER等 -->
      <div class="widget-box span2">
        <div class="widget-label">// market overview</div>
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-symbol-info.js" async>
          {{
            "symbol": "{ticker}",
            "width": "100%",
            "locale": "en",
            "colorTheme": "dark",
            "isTransparent": true
          }}
          </script>
        </div>
      </div>
      <!-- Chart -->
      <div class="widget-box span2">
        <div class="widget-label">// price chart</div>
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
          {{
            "symbol": "{ticker}",
            "width": "100%",
            "height": 420,
            "interval": "D",
            "timezone": "America/New_York",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "backgroundColor": "#0c0c10",
            "gridColor": "rgba(255,255,255,0.04)",
            "hide_side_toolbar": false,
            "allow_symbol_change": false,
            "save_image": false,
            "calendar": false
          }}
          </script>
        </div>
      </div>
      <!-- Technical Analysis -->
      <div class="widget-box">
        <div class="widget-label">// analyst rating</div>
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-technical-analysis.js" async>
          {{
            "symbol": "{ticker}",
            "width": "100%",
            "height": 400,
            "interval": "1W",
            "locale": "en",
            "colorTheme": "dark",
            "isTransparent": true
          }}
          </script>
        </div>
      </div>
      <!-- Financials -->
      <div class="widget-box">
        <div class="widget-label">// financials</div>
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-financials.js" async>
          {{
            "symbol": "{ticker}",
            "width": "100%",
            "height": 400,
            "colorTheme": "dark",
            "isTransparent": true,
            "displayMode": "regular",
            "locale": "en"
          }}
          </script>
        </div>
      </div>
    </div>

    <p class="section-title">// mention history ({len(mentions)} videos)</p>
    <div class="mention-list">
      {mention_rows if mention_rows else '<p class="no-mentions">// no mentions found in archive</p>'}
    </div>
  </main>
  <footer>// top investment voices, distilled &nbsp;·&nbsp; {now_jst} &nbsp;·&nbsp; powered by <a href="https://suno.com/@crazyzenmonk" target="_blank" rel="noopener" style="color:inherit;text-decoration:none;border-bottom:1px solid rgba(255,255,255,0.2);">CZM PROJECT</a></footer>
  <script>{_bg_canvas_js([ticker])}</script>
</body>
</html>"""


# ---- エグゼクティブサマリー生成 -----------------------------------------

def get_new_tickers(today_results: list, archive_dir: Path, today: str) -> list:
    """本日初めて登場したティッカーシンボルを検出する。"""
    historical = set()
    for json_path in sorted(archive_dir.glob("*.json")):
        if json_path.stem == today:
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            for r in data.get("results", []):
                for t in r.get("analysis", {}).get("tickers", []):
                    historical.add(t.upper().strip())
        except Exception:
            continue

    seen = {}
    for r in today_results:
        for t in r.get("analysis", {}).get("tickers", []):
            t = t.upper().strip()
            if t and t not in historical and t not in seen:
                seen[t] = {
                    "ticker":  t,
                    "channel": r["channel_name"],
                    "title":   r.get("title", ""),
                    "url":     r.get("url", ""),
                }
    return list(seen.values())


def get_undervalued_picks(today_results: list) -> list:
    """undervalued_picks フィールドからデータを集約する。"""
    items = []
    seen = set()
    for r in today_results:
        a = r.get("analysis", {})
        for t in a.get("undervalued_picks", []):
            t = t.upper().strip()
            if not t or t in seen:
                continue
            seen.add(t)
            # 理由: key_points の中から該当ティッカーに関連するものを優先
            reason = ""
            for kp in a.get("key_points", []):
                if t in kp.upper() or any(w in kp for w in ["割安", "過小評価", "無視", "買い場", "undervalued"]):
                    reason = kp
                    break
            items.append({
                "ticker":  t,
                "channel": r["channel_name"],
                "title":   r.get("title", ""),
                "url":     r.get("url", ""),
            })
    return items


def generate_executive_summary_text(
    api_key: str, results: list, n_bullish: int, n_bearish: int, n_neutral: int
) -> dict:
    """全動画サマリーを束ねてGroqでエグゼクティブサマリーを生成する。"""
    lines = []
    for r in results[:20]:  # 最大20件
        a = r.get("analysis", {})
        lines.append(
            f"[{r['channel_name']}] ({a.get('sentiment','neutral')}) {r['title']}\n"
            f"{a.get('summary_ja','')}"
        )
    combined = "\n\n".join(lines)

    prompt = f"""以下は本日の投資系YouTube動画ダイジェスト（{len(results)}本）のサマリーです。
センチメント: bullish {n_bullish} / bearish {n_bearish} / neutral {n_neutral}

{combined}

以下のJSON形式のみで回答してください（マークダウン不要、JSONオブジェクトのみ）:
{{
  "overall_summary": "本日の市場・投資テーマについて、個人投資家が把握すべき総合的な日本語サマリー（3〜5文）。主要テーマと注目ポイントを含める",
  "key_themes": ["本日全体で共通して語られた主要テーマ（最大3つ、日本語）"]
}}"""

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": _GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
        "temperature": 0.3,
    }
    for attempt in range(2):
        try:
            if attempt > 0:
                print("  [INFO] エグゼクティブサマリー再試行中 (5s待機)...")
                time.sleep(5)
            resp = requests.post(_GROQ_URL, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            print(f"  [INFO] エグゼクティブサマリー応答: {text[:120]!r}")
            text_clean = re.sub(r"```(?:json)?\s*", "", text)
            text_clean = re.sub(r"\s*```", "", text_clean).strip()
            m = re.search(r"\{[\s\S]*\}", text_clean)
            if m:
                return json.loads(m.group())
        except Exception as e:
            print(f"  [WARN] エグゼクティブサマリー生成失敗 (attempt {attempt+1}): {e}")
            traceback.print_exc()
    return {"overall_summary": "", "key_themes": []}


def build_exec_summary(
    api_key: str, results: list, archive_dir: Path, today: str,
    n_bullish: int, n_bearish: int, n_neutral: int
) -> dict:
    """エグゼクティブサマリーデータを組み立てる。"""
    print("  エグゼクティブサマリー生成中...")
    new_tickers     = get_new_tickers(results, archive_dir, today)
    undervalued     = get_undervalued_picks(results)
    time.sleep(3)  # Groq rate limit margin after per-video calls
    llm_summary     = generate_executive_summary_text(api_key, results, n_bullish, n_bearish, n_neutral)
    return {
        "new_tickers":      new_tickers,
        "undervalued_picks": undervalued,
        "overall_summary":  llm_summary.get("overall_summary", ""),
        "key_themes":       llm_summary.get("key_themes", []),
    }


# ---- ヒーロー / エグゼクティブサマリー HTML 生成 -------------------------

def _render_hero(date_str: str, n_total: int, n_bullish: int, n_bearish: int, n_neutral: int, exec_summary: dict | None) -> str:
    """ヒーローセクションHTMLを生成する。exec_summaryがあればエグゼクティブサマリーを表示。"""
    stats_html = f"""<div class="exec-stats-row">
      <span class="exec-date">{date_str}</span>
      <span class="exec-stat-total">{n_total} videos</span>
      <span class="exec-stat-bull">&#9650; {n_bullish} bullish</span>
      <span class="exec-stat-bear">&#9660; {n_bearish} bearish</span>
      <span class="exec-stat-neut">&#9679; {n_neutral} neutral</span>
    </div>"""

    if exec_summary is None:
        # アーカイブページ用: シンプルな統計表示
        return f"""<div class="hero">
    <div class="hero-prompt">
      <span class="prompt-symbol">&gt;</span>
      <span>digest</span>
      <span class="prompt-date">{date_str}</span>
    </div>
    <div class="hero-stats">
      <div class="h-stat"><div class="h-stat-num c-total">{n_total}</div><div class="h-stat-label">total</div></div>
      <div class="h-stat"><div class="h-stat-num c-bull">{n_bullish}</div><div class="h-stat-label">bullish</div></div>
      <div class="h-stat"><div class="h-stat-num c-bear">{n_bearish}</div><div class="h-stat-label">bearish</div></div>
      <div class="h-stat"><div class="h-stat-num c-neut">{n_neutral}</div><div class="h-stat-label">neutral</div></div>
    </div>
  </div>"""

    # ---- 新規ティッカーブロック ----
    new_tickers = exec_summary.get("new_tickers", [])
    root = "../" if exec_summary.get("_is_archive") else ""
    if new_tickers:
        items_html = "".join(
            f'<div class="exec-ticker-item">'
            f'<a class="ticker" href="{root}ticker/{t["ticker"]}.html">{t["ticker"]}</a>'
            f'<div class="exec-ticker-title"><a href="{t["url"]}" target="_blank" rel="noopener">{t["title"]}</a></div>'
            f'<div class="exec-ticker-ch">— {t["channel"]}</div>'
            f'</div>'
            for t in new_tickers[:4]
        )
    else:
        items_html = '<p class="exec-empty">// no new tickers today</p>'
    new_block = f'<div class="exec-block"><div class="exec-block-label">// new tickers</div>{items_html}</div>'

    # ---- 過小評価銘柄ブロック ----
    undervalued = exec_summary.get("undervalued_picks", [])
    if undervalued:
        uv_html = "".join(
            f'<div class="exec-ticker-item">'
            f'<a class="ticker" href="{root}ticker/{u["ticker"]}.html">{u["ticker"]}</a>'
            f'<div class="exec-ticker-title"><a href="{u["url"]}" target="_blank" rel="noopener">{u["title"]}</a></div>'
            f'<div class="exec-ticker-ch">— {u["channel"]}</div>'
            f'</div>'
            for u in undervalued[:4]
        )
    else:
        uv_html = '<p class="exec-empty">// no undervalued picks today</p>'
    uv_block = f'<div class="exec-block"><div class="exec-block-label">// undervalued picks</div>{uv_html}</div>'

    if n_bullish > n_bearish:
        accent = "var(--brand)"        # 緑
    elif n_bearish > n_bullish:
        accent = "var(--red)"          # 赤
    else:
        accent = "rgba(255,255,255,0.25)"  # グレー

    return f"""<section class="exec-summary">
    <div class="exec-inner" style="--exec-accent:{accent}">
      <div class="exec-header">
        <span class="exec-label">// executive summary</span>
        {stats_html}
      </div>
      <div class="exec-grid">
        {new_block}
        {uv_block}
      </div>
    </div>
  </section>"""


# ---- プレビュー用ダミーデータ -------------------------------------------

def _dummy_results(today: str) -> list[dict]:
    """--preview モード用のダミー動画データを返す。"""
    base_iso = f"{today}T10:00:00+09:00"
    items = [
        ("Financial Education",  "Why NVIDIA Could Be the Best AI Stock to Buy Right Now",          ["NVDA"],         [],           "bullish", ["個別銘柄分析"]),
        ("Everything Money",     "Is Apple Stock Overvalued? A Deep Dive into AAPL Valuation",      ["AAPL"],         [],           "neutral", ["個別銘柄分析", "決算分析"]),
        ("Sven Carlin",          "The Market Is Flashing Warning Signs — Here's What I'm Watching", ["SPY", "VIX"],   [],           "bearish", ["マクロ経済", "市場見通し"]),
        ("Tom Nash",             "Tesla Q2 Earnings Preview: What to Expect This Week",             ["TSLA"],         [],           "bullish", ["決算分析"]),
        ("Couch Investor",       "Should You Buy Archer Aviation Stock? ACHR Analysis",             ["ACHR"],         ["ACHR"],     "neutral", ["個別銘柄分析"]),
        ("Asymmetric Investing", "3 Undervalued Stocks the Market Is Completely Ignoring",          ["MNDY", "CRWD"], ["MNDY"],     "bullish", ["投資戦略", "個別銘柄分析"]),
        ("Daniel Pronk",         "Gold Is Breaking Out — What It Means for Your Portfolio",         ["GLD", "GDX"],   [],           "bullish", ["マクロ経済"]),
        ("Chris Sain",           "Monday.com Stock: Why I Just Added to My Position",               ["MNDY"],         ["MNDY"],     "bullish", ["個別銘柄分析"]),
    ]
    results = []
    for ch, title, tickers, undervalued, sentiment, topics in items:
        results.append({
            "channel_name": ch,
            "video_id":     "dummyVideoId",
            "title":        title,
            "url":          "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "published":    base_iso,
            "price_snapshot": {},
            "analysis": {
                "summary_ja":       "これはプレビューモード用のダミー要約テキストです。実際の動画内容は反映されていません。デザイン確認用にご利用ください。",
                "tickers":          tickers,
                "undervalued_picks": undervalued,
                "key_points":       ["ダミーポイント①：実際のデータは本番実行時に生成されます", "ダミーポイント②：デザイン確認専用のサンプルテキストです"],
                "sentiment":        sentiment,
                "topics":           topics,
                "importance":       3,
            },
        })
    return results


# ---- メイン ------------------------------------------------------------

def main() -> None:
    preview_mode = "--preview" in sys.argv

    # ---- プレビューモード ------------------------------------------------
    if preview_mode:
        print("=== PREVIEW MODE ===  (API呼び出しなし・ダミーデータで生成)")
        today = datetime.now(JST).strftime("%Y-%m-%d")
        all_results = _dummy_results(today)
        channels = load_channels()

        DOCS_DIR.mkdir(exist_ok=True)
        archive_dir = DOCS_DIR / "archive"
        archive_dir.mkdir(exist_ok=True)

        # ダミーのエグゼクティブサマリー
        n_bull = sum(1 for r in all_results if r["analysis"]["sentiment"] == "bullish")
        n_bear = sum(1 for r in all_results if r["analysis"]["sentiment"] == "bearish")
        n_neut = len(all_results) - n_bull - n_bear
        dummy_exec = {
            "new_tickers": [
                {"ticker": "ACHR", "channel": "Couch Investor",
                 "title": "Should You Buy Archer Aviation Stock? ACHR Analysis",
                 "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            ],
            "undervalued_picks": [
                {"ticker": "MNDY", "channel": "Asymmetric Investing",
                 "title": "3 Undervalued Stocks the Market Is Completely Ignoring",
                 "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            ],
        }

        # トップページ
        with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as f:
            f.write(generate_html(all_results, today, exec_summary=dummy_exec))
        # channelsページ
        with open(DOCS_DIR / "channels.html", "w", encoding="utf-8") as f:
            f.write(generate_channels_html(channels))
        # archiveの日別ページ（今日分＋ダミー2日分）
        dummy_dates = [
            today,
            (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d"),
            (datetime.now(JST) - timedelta(days=2)).strftime("%Y-%m-%d"),
        ]
        for d in dummy_dates:
            with open(archive_dir / f"{d}.html", "w", encoding="utf-8") as f:
                f.write(generate_html(_dummy_results(d), d, is_archive=True))
        # archive一覧ページ
        with open(archive_dir / "index.html", "w", encoding="utf-8") as f:
            f.write(generate_archive_index_html(sorted(dummy_dates, reverse=True)))

        index_path = DOCS_DIR / "index.html"
        print(f"\n[OK] プレビュー生成完了: {index_path}")
        print("  ブラウザで開いてください:")
        print(f"  file:///{index_path.as_posix()}")

        import webbrowser
        webbrowser.open(index_path.as_uri())
        return

    # ---- 通常モード ------------------------------------------------------
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "環境変数 GROQ_API_KEY が設定されていません。\n"
            "GitHub Actions の場合: Settings → Secrets → GROQ_API_KEY を追加してください。\n"
            "ローカルの場合: export GROQ_API_KEY=gsk_..."
        )

    client = api_key  # REST API キーをそのまま渡す

    # 1. チャンネルリストの読み込み & チャンネルID解決
    channels = load_channels()
    channels = resolve_channel_ids(channels)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    all_results: list[dict] = []

    # 2. 各チャンネルの新着動画を処理
    for ch in channels:
        if not ch.get("channel_id"):
            print(f"[SKIP] {ch['name']} (channel_id 未解決)")
            continue

        print(f"\n[{ch['name']}] 新着動画を取得中...")
        videos = get_recent_videos(ch["channel_id"])

        if not videos:
            print("  → 新着なし")
            continue

        for video in videos:
            if is_short(video):
                print(f"  [SKIP] ショート動画をスキップ: {video['title'][:50]}")
                continue
            if ch.get("lang") == "en" and is_german(video):
                print(f"  [SKIP] ドイツ語コンテンツをスキップ: {video['title'][:50]}")
                continue
            print(f"  処理中: {video['title'][:60]}...")

            try:
                analysis = summarize_video(
                    client,
                    video["title"],
                    video["description"],
                )
            except Exception as e:
                print(f"  [ERROR] 要約失敗: {type(e).__name__}: {e}")
                analysis = {
                    "summary_ja": "要約の取得に失敗しました。",
                    "tickers": [], "key_points": [],
                    "sentiment": "neutral", "topics": [], "importance": 1,
                }

            all_results.append({
                "channel_name": ch["name"],
                "video_id":     video["video_id"],
                "title":        video["title"],
                "url":          video["url"],
                "published":    video["published"],
                "analysis":     analysis,
            })

            time.sleep(2)  # Groq レート制限対策（30 RPM = 2秒間隔）

    # 当日言及されたティッカーの株価スナップショットを取得
    print("\n株価スナップショットを取得中...")
    all_tickers = set()
    for r in all_results:
        for t in r.get("analysis", {}).get("tickers", []):
            all_tickers.add(t.upper().strip())

    price_snapshot: dict[str, float | None] = {}
    for t in sorted(all_tickers):
        price = get_price_on_date(t, today)
        price_snapshot[t] = price
        print(f"  {t}: {price}")
        time.sleep(0.2)

    # price_snapshot を各 result に付与
    for r in all_results:
        r["price_snapshot"] = price_snapshot

    # 3. HTML & JSON を生成・保存
    print("\nHTML/JSON ファイル生成中...")
    DOCS_DIR.mkdir(exist_ok=True)
    archive_dir = DOCS_DIR / "archive"
    archive_dir.mkdir(exist_ok=True)

    # アーカイブ: 当日分の HTML / JSON を保存
    print(f"  アーカイブHTML生成: archive/{today}.html")
    archive_html_path = archive_dir / f"{today}.html"
    with open(archive_html_path, "w", encoding="utf-8") as f:
        f.write(generate_html(all_results, today, is_archive=True))

    print(f"  アーカイブJSON保存: archive/{today}.json")
    with open(archive_dir / f"{today}.json", "w", encoding="utf-8") as f:
        json.dump({"date": today, "results": all_results}, f, ensure_ascii=False, indent=2)

    # アーカイブ一覧ページを更新
    print("  アーカイブ一覧ページ生成中...")
    all_dates = sorted(
        [f.stem for f in archive_dir.glob("*.html") if f.stem != "index"],
        reverse=True,
    )
    with open(archive_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(generate_archive_index_html(all_dates))

    # チャンネルページを生成
    print("  チャンネルページ生成中...")
    with open(DOCS_DIR / "channels.html", "w", encoding="utf-8") as f:
        f.write(generate_channels_html(channels))

    # エグゼクティブサマリーを生成
    n_bullish = sum(1 for r in all_results if r.get("analysis", {}).get("sentiment") == "bullish")
    n_bearish = sum(1 for r in all_results if r.get("analysis", {}).get("sentiment") == "bearish")
    n_neutral = len(all_results) - n_bullish - n_bearish
    exec_summary = build_exec_summary(
        client, all_results, archive_dir, today, n_bullish, n_bearish, n_neutral
    )

    # メインページ (index.html) を更新
    print("  メインページ(index.html)生成中...")
    with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(generate_html(all_results, today, exec_summary=exec_summary))

    # latest.json（後方互換）
    print("  latest.json保存中...")
    with open(DOCS_DIR / "latest.json", "w", encoding="utf-8") as f:
        json.dump({"date": today, "results": all_results}, f, ensure_ascii=False, indent=2)

    # ティッカーページ生成: 全アーカイブJSONを走査してティッカー別に集計
    print("  ティッカーページ生成中...")
    ticker_mentions: dict[str, list[dict]] = {}
    for json_path in sorted(archive_dir.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        date = data.get("date", json_path.stem)
        for result in data.get("results", []):
            tickers = result.get("analysis", {}).get("tickers", [])
            for t in tickers:
                t = t.upper().strip()
                if not t:
                    continue
                ticker_mentions.setdefault(t, []).append({
                    "date":         date,
                    "channel_name": result.get("channel_name", ""),
                    "title":        result.get("title", ""),
                    "url":          result.get("url", ""),
                    "page_path":    f"archive/{date}.html",
                    "price_on_date": result.get("price_snapshot", {}).get(t),
                })

    # 各ティッカーの現在株価を取得してページ生成
    ticker_dir = DOCS_DIR / "ticker"
    ticker_dir.mkdir(exist_ok=True)
    for ticker, mentions in ticker_mentions.items():
        mentions_sorted = sorted(mentions, key=lambda x: x["date"], reverse=True)
        current_price = get_current_price(ticker)
        with open(ticker_dir / f"{ticker}.html", "w", encoding="utf-8") as f:
            f.write(generate_ticker_page(ticker, mentions_sorted, current_price))
    print(f"  Tickers: {len(ticker_mentions)} ページ生成")

    print(f"\n✓ 完了: {len(all_results)} 本の動画を処理しました。")
    print(f"  HTML:    {DOCS_DIR / 'index.html'}")
    print(f"  Archive: {archive_html_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n[FATAL] 予期しないエラーが発生しました:")
        traceback.print_exc()
        raise
