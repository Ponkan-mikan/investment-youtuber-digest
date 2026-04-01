"""
Investment YouTube Daily Digest Generator
毎日の投資系YouTube動画を要約してHTMLレポートを生成する
"""
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
import anthropic

# ---- 定数 ----------------------------------------------------------------
JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = BASE_DIR / "docs"

# 何時間以内の動画を対象にするか（デフォルト48h = 投稿漏れを防ぐため少し余裕を持たせる）
LOOKBACK_HOURS = 48

# トランスクリプトの最大文字数（Claude APIのコンテキスト節約のため）
TRANSCRIPT_MAX_CHARS = 8000

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
    feed = feedparser.parse(rss_url)
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
    if _SHORT_RE.search(f"{video['title']} {video['description']}"):
        return True
    d = video.get("duration_seconds", 0)
    if d and 0 < d < 61:
        return True
    return False


# ---- 日時フォーマット ---------------------------------------------------

def format_pub_datetime(iso_str: str) -> str:
    """UTC ISO文字列をJST日時文字列に変換する。"""
    try:
        return datetime.fromisoformat(iso_str).astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    except Exception:
        return iso_str[:10]


# ---- トランスクリプト ---------------------------------------------------

def get_transcript(video_id: str) -> str | None:
    """YouTube動画の英語字幕を取得する。失敗時は None を返す。"""
    # --- v1.x: 言語指定あり ---
    try:
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id, languages=["en", "en-US", "en-GB"])
        text = " ".join(
            s.text if hasattr(s, "text") else str(s.get("text", ""))
            for s in transcript
        )
        if text.strip():
            return text[:TRANSCRIPT_MAX_CHARS]
    except Exception as e:
        print(f"  [WARN] transcript tier1 失敗 {video_id}: {type(e).__name__}: {e}")

    # --- v1.x: 言語指定なし ---
    try:
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id)
        text = " ".join(
            s.text if hasattr(s, "text") else str(s.get("text", ""))
            for s in transcript
        )
        if text.strip():
            return text[:TRANSCRIPT_MAX_CHARS]
    except Exception as e:
        print(f"  [WARN] transcript tier2 失敗 {video_id}: {type(e).__name__}: {e}")

    return None


# ---- Claude API 要約 ---------------------------------------------------

SUMMARY_PROMPT_TEMPLATE = """\
以下の投資系YouTube動画から、個人投資家にとって重要な情報を抽出してください。

タイトル: {title}

概要欄:
{description}

{transcript_section}

以下のJSON形式のみで回答してください（説明文は不要）:
{{
  "summary_ja": "動画の主要な内容を日本語で3〜5文で要約",
  "tickers": ["言及された株式ティッカーシンボル（例: AAPL, NVDA）のリスト。なければ空配列"],
  "key_points": ["投資判断に役立つ重要ポイントを日本語で箇条書き（最大5つ）"],
  "sentiment": "bullish か bearish か neutral のいずれか",
  "topics": ["該当するカテゴリ（例: 個別銘柄分析, マクロ経済, 投資戦略, 決算分析, 市場見通し）"],
  "importance": 投資家にとっての重要度スコア（1〜5の整数。5が最重要、投資に無関係なら1）
}}"""


def summarize_video(
    client: anthropic.Anthropic,
    title: str,
    description: str,
    transcript: str | None,
) -> dict:
    """Claude API を使って動画の投資情報を要約する。"""
    transcript_section = (
        f"トランスクリプト（冒頭 {TRANSCRIPT_MAX_CHARS} 文字）:\n{transcript}"
        if transcript
        else "（トランスクリプトなし）"
    )
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        title=title,
        description=description[:2000],
        transcript_section=transcript_section,
    )
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    # JSON ブロックを抽出
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    # パース失敗時のフォールバック
    return {
        "summary_ja": text[:500],
        "tickers": [],
        "key_points": [],
        "sentiment": "neutral",
        "topics": [],
        "importance": 1,
    }


# ---- HTML 生成 ---------------------------------------------------------

_SENT_LABEL = {"bullish": "▲ Bullish", "bearish": "▼ Bearish", "neutral": "● Neutral"}


def _render_card(r: dict) -> str:
    a          = r.get("analysis", {})
    importance = max(1, min(5, int(a.get("importance", 1))))
    sentiment  = a.get("sentiment", "neutral")
    tickers    = a.get("tickers", [])
    topics     = a.get("topics", [])
    key_points = a.get("key_points", [])
    pub_date   = format_pub_datetime(r.get("published", ""))

    sent_label  = _SENT_LABEL.get(sentiment, sentiment)
    imp_pct     = importance * 20
    ticker_html = "".join(f'<span class="ticker">{t}</span>' for t in tickers)
    topic_html  = "".join(f'<span class="topic">{t}</span>'  for t in topics)
    tags_block  = f'<div class="tags">{ticker_html}{topic_html}</div>' if (tickers or topics) else ""

    if key_points:
        items = "".join(f"<li>{p}</li>" for p in key_points)
        points_block = (
            f'<details class="points-details">'
            f'<summary>Key Points <span class="points-count">{len(key_points)}</span></summary>'
            f'<ul class="points">{items}</ul>'
            f'</details>'
        )
    else:
        points_block = ""

    return f"""<article class="card sent-{sentiment} imp-{importance}" data-sentiment="{sentiment}" data-importance="{importance}">
  <div class="card-top">
    <span class="channel-name">{r['channel_name']}</span>
    <span class="badge-sent {sentiment}">{sent_label}</span>
  </div>
  <h3><a href="{r['url']}" target="_blank" rel="noopener">{r['title']}</a></h3>
  {tags_block}
  <div class="imp-track"><div class="imp-fill" style="width:{imp_pct}%"></div></div>
  <p class="summary">{a.get('summary_ja', '')}</p>
  {points_block}
  <div class="card-footer">
    <time>{pub_date}</time>
    <a class="watch-btn" href="{r['url']}" target="_blank" rel="noopener">Watch ↗</a>
  </div>
</article>"""


def generate_html(results: list[dict], date_str: str, is_archive: bool = False) -> str:
    results_sorted = sorted(
        results,
        key=lambda x: int(x.get("analysis", {}).get("importance", 1)),
        reverse=True,
    )

    n_bullish = sum(1 for r in results if r.get("analysis", {}).get("sentiment") == "bullish")
    n_bearish = sum(1 for r in results if r.get("analysis", {}).get("sentiment") == "bearish")
    n_neutral = len(results) - n_bullish - n_bearish
    now_jst   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    cards = "\n".join(_render_card(r) for r in results_sorted)
    if not cards:
        cards = '<div class="empty"><p>新着動画はありません</p></div>'

    nav_links = (
        "<a href='../index.html' class='nav-link'>← Latest</a>"
        "<a href='index.html' class='nav-link'>All Archives</a>"
        if is_archive else
        "<a href='archive/index.html' class='nav-link'>Archive →</a>"
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Investment Digest — {date_str}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;700&family=Noto+Sans+JP:wght@400;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:       #0a0a0f;
      --bg-card:  #12121a;
      --bg-hdr:   rgba(10,10,15,0.92);
      --surface:  rgba(255,255,255,0.04);
      --surface2: rgba(255,255,255,0.07);
      --border:   rgba(255,255,255,0.06);
      --text:     #e8e8f0;
      --muted:    #8888a8;
      --bullish:  #00d4aa;
      --bearish:  #ff4757;
      --neutral:  #6c7293;
      --gold:     #f0b429;
      --blue:     #4f8ef7;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      font-family: 'Inter', 'Noto Sans JP', -apple-system, sans-serif;
      background: var(--bg); color: var(--text); min-height: 100vh;
      background-image:
        radial-gradient(ellipse 70% 45% at 50% -5%, rgba(0,212,170,0.07) 0%, transparent 60%),
        radial-gradient(ellipse 50% 35% at 85% 95%, rgba(79,142,247,0.05) 0%, transparent 55%);
    }}
    ::-webkit-scrollbar {{ width: 5px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: #2a2a3e; border-radius: 3px; }}

    /* ── Header ── */
    header {{
      position: sticky; top: 0; z-index: 100;
      background: var(--bg-hdr); backdrop-filter: blur(24px);
      border-bottom: 1px solid var(--border);
      padding: 0 clamp(16px,4vw,48px);
      transition: background 200ms ease;
    }}
    header.scrolled {{ background: rgba(10,10,15,0.98); }}
    .header-inner {{
      max-width: 1440px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between;
      height: 64px; gap: 16px;
      transition: height 200ms ease;
    }}
    header.scrolled .header-inner {{ height: 52px; }}

    /* Rotating orbit logo */
    .logo {{ display: flex; align-items: center; gap: 10px; white-space: nowrap; }}
    .logo-orbit {{
      width: 34px; height: 34px; border-radius: 10px;
      position: relative; overflow: hidden; flex-shrink: 0;
    }}
    .logo-orbit::before {{
      content: '';
      position: absolute; inset: -40%;
      background: conic-gradient(from 0deg, #00d4aa, #6366f1, #ff4757, #00d4aa);
      animation: orbit 4s linear infinite;
    }}
    .logo-inner {{
      position: absolute; inset: 2px; background: #1a1a2e;
      border-radius: 8px; display: flex; align-items: center;
      justify-content: center; font-size: 0.58rem; font-weight: 800;
      color: #e8e8f0; letter-spacing: 0.04em; z-index: 1;
    }}
    @keyframes orbit {{ to {{ transform: rotate(360deg); }} }}
    .logo-text {{
      font-size: 1rem; font-weight: 700; letter-spacing: -0.02em;
    }}

    .header-meta {{
      display: flex; align-items: center; gap: 10px;
      font-size: 0.75rem; color: var(--muted); flex-wrap: nowrap;
    }}
    .stat-pill {{
      display: flex; align-items: center; gap: 5px;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 20px; padding: 4px 11px; font-size: 0.73rem;
    }}
    .dot {{ width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }}
    .dot-bull {{ background: var(--bullish); box-shadow: 0 0 7px var(--bullish); }}
    .dot-bear {{ background: var(--bearish); box-shadow: 0 0 7px var(--bearish); }}
    .dot-neut {{ background: var(--neutral); box-shadow: 0 0 6px var(--neutral); }}
    .nav-link {{
      font-size: 0.75rem; font-weight: 500; color: var(--muted);
      text-decoration: none; padding: 5px 12px;
      border: 1px solid var(--border); border-radius: 20px;
      transition: all 200ms ease;
    }}
    .nav-link:hover {{ color: var(--text); border-color: rgba(255,255,255,0.18); background: var(--surface); }}

    /* ── Hero ── */
    .hero {{
      text-align: center;
      padding: clamp(48px,7vw,96px) 16px clamp(28px,4vw,52px);
    }}
    .hero-date {{
      display: inline-block; font-size: 0.7rem; font-weight: 600;
      letter-spacing: 0.14em; text-transform: uppercase;
      color: var(--bullish); opacity: 0.8;
      background: rgba(0,212,170,0.08); border: 1px solid rgba(0,212,170,0.2);
      border-radius: 20px; padding: 4px 14px; margin-bottom: 22px;
    }}
    .hero h1 {{
      font-size: clamp(2.4rem, 5.5vw, 3.8rem); font-weight: 700;
      letter-spacing: -0.03em; line-height: 1.08;
      background: linear-gradient(135deg, #fff 0%, #a8c5ff 45%, #00d4aa 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      background-clip: text; margin-bottom: 14px;
    }}
    .hero-sub {{ font-size: 0.88rem; color: var(--muted); opacity: 0.7; }}
    .hero-stats {{
      display: flex; justify-content: center; gap: clamp(20px,4vw,56px);
      margin-top: 32px; flex-wrap: wrap;
    }}
    .h-stat {{ text-align: center; }}
    .h-stat-num {{
      font-size: clamp(1.7rem, 4vw, 2.4rem); font-weight: 700;
      letter-spacing: -0.04em; line-height: 1;
    }}
    .h-stat-num.c-total {{ color: #fff; }}
    .h-stat-num.c-bull  {{ color: var(--bullish); text-shadow: 0 0 24px rgba(0,212,170,0.4); }}
    .h-stat-num.c-bear  {{ color: var(--bearish);  text-shadow: 0 0 24px rgba(255,71,87,0.4); }}
    .h-stat-num.c-neut  {{ color: var(--neutral);  text-shadow: 0 0 20px rgba(108,114,147,0.35); }}
    .h-stat-label {{ font-size: 0.68rem; color: var(--muted); margin-top: 5px; text-transform: uppercase; letter-spacing: 0.09em; }}

    /* ── Filters ── */
    .filters {{
      max-width: 1440px; margin: 0 auto 28px;
      padding: 0 clamp(16px,4vw,48px);
      display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    }}
    .filter-segment {{
      display: flex; background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 999px; padding: 3px; gap: 2px;
    }}
    .filter-btn {{
      background: transparent; border: none; color: var(--muted);
      border-radius: 999px; padding: 6px 16px;
      font-size: 0.77rem; font-family: 'Inter', sans-serif; cursor: pointer;
      transition: all 200ms ease; white-space: nowrap;
    }}
    .filter-btn:hover {{ color: var(--text); transform: scale(1.02); }}
    .filter-btn.active {{
      color: var(--text); background: rgba(255,255,255,0.1);
      backdrop-filter: blur(8px);
    }}
    .filter-btn.f-bull.active {{ color: var(--bullish); background: rgba(0,212,170,0.12); }}
    .filter-btn.f-bear.active {{ color: var(--bearish); background: rgba(255,71,87,0.12); }}
    .filter-btn.f-neut.active {{ color: var(--neutral); background: rgba(108,114,147,0.14); }}
    .filter-divider {{ width: 1px; height: 22px; background: rgba(255,255,255,0.08); margin: 0 2px; }}

    /* ── Grid ── */
    .grid-wrap {{ max-width: 1440px; margin: 0 auto; padding: 0 clamp(16px,4vw,48px) 80px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
      gap: 16px;
    }}
    #no-results {{ display: none; text-align: center; color: var(--muted); padding: 80px 20px; grid-column: 1/-1; }}
    .empty {{ text-align: center; color: var(--muted); padding: 80px 20px; }}

    /* ── Cards ── */
    .card {{
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 16px; padding: 20px 22px;
      position: relative; overflow: hidden;
      opacity: 0; transform: translateY(20px);
      transition: opacity 400ms ease, transform 400ms ease,
                  box-shadow 200ms ease, border-color 200ms ease;
    }}
    .card.visible {{ opacity: 1; transform: translateY(0); }}
    .card.hidden  {{ display: none !important; }}
    .card:hover   {{ transform: translateY(-3px); }}

    /* Left sentiment sidebar */
    .card.sent-bullish {{ box-shadow: inset 4px 0 0 var(--bullish); }}
    .card.sent-bearish {{ box-shadow: inset 4px 0 0 var(--bearish); }}
    .card.sent-neutral {{ box-shadow: inset 4px 0 0 var(--neutral); }}
    .card.sent-bullish:hover {{ box-shadow: inset 4px 0 0 var(--bullish), 0 8px 32px rgba(0,212,170,0.1); border-color: rgba(0,212,170,0.22); }}
    .card.sent-bearish:hover {{ box-shadow: inset 4px 0 0 var(--bearish), 0 8px 32px rgba(255,71,87,0.1);  border-color: rgba(255,71,87,0.22); }}
    .card.sent-neutral:hover {{ box-shadow: inset 4px 0 0 var(--neutral), 0 8px 24px rgba(108,114,147,0.08); border-color: rgba(108,114,147,0.2); }}

    .card-top {{
      display: flex; justify-content: space-between; align-items: center;
      gap: 8px; margin-bottom: 10px;
    }}
    .channel-name {{
      font-size: 0.68rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.1em; color: var(--muted);
    }}
    .badge-sent {{
      font-size: 0.67rem; font-weight: 600; letter-spacing: 0.03em;
      border-radius: 20px; padding: 2px 9px; border: 1px solid; flex-shrink: 0;
    }}
    .badge-sent.bullish {{ color: var(--bullish); border-color: rgba(0,212,170,0.35); background: rgba(0,212,170,0.08); }}
    .badge-sent.bearish {{ color: var(--bearish); border-color: rgba(255,71,87,0.35);  background: rgba(255,71,87,0.08); }}
    .badge-sent.neutral {{ color: var(--neutral); border-color: rgba(108,114,147,0.35); background: rgba(108,114,147,0.08); }}

    h3 {{ font-size: 0.93rem; font-weight: 600; line-height: 1.52; margin-bottom: 10px; }}
    h3 a {{ color: var(--text); text-decoration: none; }}
    h3 a:hover {{ color: #a8c5ff; }}

    /* Tags */
    .tags {{ display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }}
    .ticker {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.72rem; font-weight: 700;
      background: rgba(0,212,170,0.07); color: var(--bullish);
      border: 1px solid rgba(0,212,170,0.2);
      padding: 2px 8px; border-radius: 5px; letter-spacing: 0.06em;
    }}
    .topic {{
      font-size: 0.7rem;
      background: rgba(108,114,147,0.1); color: #a0a8c8;
      border: 1px solid rgba(108,114,147,0.2);
      padding: 2px 8px; border-radius: 5px;
    }}

    /* Importance progress bar */
    .imp-track {{
      height: 3px; background: rgba(255,255,255,0.05);
      border-radius: 2px; margin: 6px 0 14px; overflow: hidden;
    }}
    .imp-fill {{
      height: 100%; border-radius: 2px;
      background: linear-gradient(90deg, var(--neutral), #a0a8c8);
      transition: width 600ms ease;
    }}
    .card.imp-5 .imp-fill {{ background: linear-gradient(90deg, var(--gold), #fbbf24); }}
    .card.imp-4 .imp-fill {{ background: linear-gradient(90deg, var(--blue), #7eb3ff); }}
    .card.imp-3 .imp-fill {{ background: linear-gradient(90deg, #5a5f7a, #7a80a0); }}

    /* Summary */
    .summary {{
      font-size: 0.868rem; line-height: 1.82; color: #9090b0;
      margin-bottom: 10px;
    }}

    /* Key points accordion */
    .points-details {{ margin-bottom: 10px; }}
    .points-details > summary {{
      font-size: 0.74rem; font-weight: 600; color: var(--muted);
      cursor: pointer; display: flex; align-items: center; gap: 6px;
      padding: 3px 0; list-style: none; user-select: none;
      transition: color 200ms;
    }}
    .points-details > summary::-webkit-details-marker {{ display: none; }}
    .points-details > summary::before {{
      content: '›'; font-size: 1.1rem; line-height: 1;
      display: inline-block; transition: transform 200ms ease;
    }}
    .points-details[open] > summary::before {{ transform: rotate(90deg); }}
    .points-details > summary:hover {{ color: var(--text); }}
    .points-count {{
      background: rgba(255,255,255,0.07); border-radius: 4px;
      padding: 1px 6px; font-size: 0.65rem;
    }}
    .points {{ list-style: none; padding: 8px 0 0; }}
    .points li {{
      font-size: 0.82rem; color: var(--muted); line-height: 1.8;
      padding-left: 14px; position: relative; margin-bottom: 2px;
    }}
    .points li::before {{
      content: ''; position: absolute; left: 0; top: 0.68em;
      width: 5px; height: 5px; border-radius: 50%;
      background: var(--neutral);
    }}
    .card.sent-bullish .points li::before {{ background: var(--bullish); opacity: 0.7; }}
    .card.sent-bearish .points li::before {{ background: var(--bearish); opacity: 0.7; }}

    /* Card footer */
    .card-footer {{
      display: flex; justify-content: space-between; align-items: center;
      padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.05);
      margin-top: 8px;
    }}
    .card-footer time {{ font-size: 0.68rem; color: var(--muted); }}
    .watch-btn {{
      font-size: 0.71rem; font-weight: 600; color: var(--muted);
      text-decoration: none; padding: 5px 13px; border-radius: 8px;
      border: 1px solid var(--border); background: transparent;
      transition: all 200ms ease;
    }}
    .watch-btn:hover {{
      color: #0a0a0f; background: var(--bullish); border-color: var(--bullish);
    }}

    /* ── Footer ── */
    footer {{
      text-align: center; padding: 24px 16px;
      border-top: 1px solid var(--border);
      font-size: 0.73rem; color: var(--muted);
    }}

    /* ── Responsive ── */
    @media (max-width: 640px) {{
      .header-meta .stat-pill {{ display: none; }}
      .hero h1 {{ font-size: 2rem; }}
      .grid {{ grid-template-columns: 1fr; }}
      .filters {{ flex-wrap: nowrap; overflow-x: auto; padding-bottom: 6px; }}
      .filters::-webkit-scrollbar {{ display: none; }}
    }}
  </style>
</head>
<body>

  <header id="hdr">
    <div class="header-inner">
      <div class="logo">
        <div class="logo-orbit"><span class="logo-inner">ID</span></div>
        <span class="logo-text">Investment Digest</span>
      </div>
      <div class="header-meta">
        <div class="stat-pill"><span class="dot dot-bull"></span>{n_bullish} Bullish</div>
        <div class="stat-pill"><span class="dot dot-bear"></span>{n_bearish} Bearish</div>
        <div class="stat-pill"><span class="dot dot-neut"></span>{n_neutral} Neutral</div>
        <span style="opacity:0.5">{now_jst}</span>
        {nav_links}
      </div>
    </div>
  </header>

  <section class="hero">
    <div class="hero-date">{date_str}</div>
    <h1>Investment<br>YouTube Digest</h1>
    <p class="hero-sub">米国投資系YouTubeチャンネル 新着動画まとめ</p>
    <div class="hero-stats">
      <div class="h-stat"><div class="h-stat-num c-total">{len(results)}</div><div class="h-stat-label">Total</div></div>
      <div class="h-stat"><div class="h-stat-num c-bull">{n_bullish}</div><div class="h-stat-label">Bullish</div></div>
      <div class="h-stat"><div class="h-stat-num c-bear">{n_bearish}</div><div class="h-stat-label">Bearish</div></div>
      <div class="h-stat"><div class="h-stat-num c-neut">{n_neutral}</div><div class="h-stat-label">Neutral</div></div>
    </div>
  </section>

  <div class="filters">
    <div class="filter-segment">
      <button class="filter-btn active" data-filter="all">All</button>
      <button class="filter-btn f-bull" data-filter="bullish">▲ Bullish</button>
      <button class="filter-btn f-bear" data-filter="bearish">▼ Bearish</button>
      <button class="filter-btn f-neut" data-filter="neutral">● Neutral</button>
    </div>
    <div class="filter-divider"></div>
    <div class="filter-segment">
      <button class="filter-btn" data-imp="5">Top Picks</button>
      <button class="filter-btn" data-imp="4">Important+</button>
    </div>
  </div>

  <div class="grid-wrap">
    <div class="grid" id="grid">
      {cards}
      <div id="no-results">条件に一致する動画がありません</div>
    </div>
  </div>

  <footer>Investment Digest &nbsp;·&nbsp; {now_jst} &nbsp;·&nbsp; Powered by Claude AI</footer>

  <script>
    // Sticky header shrink
    const hdr = document.getElementById('hdr');
    window.addEventListener('scroll', () => {{
      hdr.classList.toggle('scrolled', window.scrollY > 60);
    }}, {{ passive: true }});

    // IntersectionObserver card fade-in
    const observer = new IntersectionObserver((entries) => {{
      entries.forEach(entry => {{
        if (entry.isIntersecting) {{
          entry.target.classList.add('visible');
          observer.unobserve(entry.target);
        }}
      }});
    }}, {{ threshold: 0.08 }});
    document.querySelectorAll('.card').forEach((c, i) => {{
      c.style.transitionDelay = Math.min(i * 40, 320) + 'ms';
      observer.observe(c);
    }});

    // Filters
    let activeSentiment = 'all';
    let activeImp = 0;
    const allCards = document.querySelectorAll('.card');

    function applyFilters() {{
      let visible = 0;
      allCards.forEach(c => {{
        const matchS = activeSentiment === 'all' || c.dataset.sentiment === activeSentiment;
        const matchI = activeImp === 0 || parseInt(c.dataset.importance) >= activeImp;
        if (matchS && matchI) {{ c.classList.remove('hidden'); visible++; }}
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

    document.querySelectorAll('[data-imp]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const val = parseInt(btn.dataset.imp);
        if (activeImp === val) {{
          activeImp = 0; btn.classList.remove('active');
        }} else {{
          btn.closest('.filter-segment').querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active'); activeImp = val;
        }}
        applyFilters();
      }});
    }});
  </script>

</body>
</html>"""


# ---- アーカイブ一覧ページ -----------------------------------------------

def generate_archive_index_html(dates: list[str]) -> str:
    """過去の日次ダイジェスト一覧ページを生成する。"""
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    rows = "".join(
        f'<a class="archive-row" href="{d}.html">'
        f'<span class="arc-date">{d}</span>'
        f'<span class="arc-arrow">→</span>'
        f'</a>'
        for d in dates
    )
    if not rows:
        rows = '<p class="arc-empty">アーカイブはまだありません。</p>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Archive — Investment Digest</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;700&family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #0a0a0f; --bg-card: #12121a; --border: rgba(255,255,255,0.07);
      --bullish: #00d4aa; --text: #e8e8f0; --muted: #8888a8;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Inter', 'Noto Sans JP', sans-serif;
      background: var(--bg); color: var(--text); min-height: 100vh;
      background-image: radial-gradient(ellipse 80% 50% at 50% -10%, rgba(0,212,170,0.06) 0%, transparent 60%);
    }}
    header {{
      position: sticky; top: 0; z-index: 100;
      background: rgba(10,10,15,0.85); backdrop-filter: blur(20px);
      border-bottom: 1px solid var(--border);
      padding: 0 clamp(16px,4vw,48px);
    }}
    .header-inner {{
      max-width: 800px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between;
      height: 64px;
    }}
    .logo {{ display: flex; align-items: center; gap: 10px; font-size: 1.05rem; font-weight: 700; color: var(--text); text-decoration: none; }}
    .logo-orbit {{
      position: relative; width: 32px; height: 32px; flex-shrink: 0;
    }}
    .logo-orbit::before {{
      content: ''; position: absolute; inset: 0; border-radius: 50%;
      background: conic-gradient(var(--bullish) 0deg, transparent 120deg, rgba(0,212,170,0.3) 240deg, var(--bullish) 360deg);
      animation: orbit 3s linear infinite;
    }}
    .logo-orbit::after {{
      content: ''; position: absolute; inset: 3px; border-radius: 50%;
      background: var(--bg-card);
    }}
    @keyframes orbit {{ to {{ transform: rotate(360deg); }} }}
    .nav-link {{
      font-size: 0.78rem; font-weight: 500; color: var(--muted);
      text-decoration: none; padding: 5px 12px;
      border: 1px solid var(--border); border-radius: 20px;
      transition: all 0.15s ease;
    }}
    .nav-link:hover {{ color: var(--text); border-color: rgba(255,255,255,0.2); background: rgba(255,255,255,0.05); }}
    main {{ max-width: 800px; margin: 0 auto; padding: clamp(40px,6vw,80px) clamp(16px,4vw,48px) 80px; }}
    h1 {{
      font-size: clamp(1.8rem,4vw,2.4rem); font-weight: 700; letter-spacing: -0.03em;
      background: linear-gradient(135deg, #fff 0%, #80ffe8 60%, var(--bullish) 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      background-clip: text; margin-bottom: 8px;
    }}
    .subtitle {{ color: var(--muted); font-size: 0.88rem; margin-bottom: 40px; }}
    .archive-list {{ display: flex; flex-direction: column; gap: 6px; }}
    .archive-row {{
      display: flex; justify-content: space-between; align-items: center;
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 10px; padding: 14px 20px; text-decoration: none;
      transition: all 0.18s ease;
    }}
    .archive-row:hover {{
      background: rgba(0,212,170,0.05); border-color: rgba(0,212,170,0.3);
      transform: translateX(4px);
    }}
    .arc-date {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.95rem; font-weight: 600; color: var(--text);
    }}
    .arc-arrow {{ color: var(--bullish); font-size: 1rem; }}
    .arc-empty {{ color: var(--muted); text-align: center; padding: 40px; }}
    footer {{ text-align: center; padding: 24px; border-top: 1px solid var(--border); font-size: 0.72rem; color: var(--muted); margin-top: 40px; }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <a href="../index.html" class="logo"><div class="logo-orbit"></div>Investment Digest</a>
      <a href="../index.html" class="nav-link">← Latest</a>
    </div>
  </header>
  <main>
    <h1>Archive</h1>
    <p class="subtitle">過去の投資YouTube日次ダイジェスト一覧 — {len(dates)} 件</p>
    <div class="archive-list">{rows}</div>
  </main>
  <footer>Investment Digest · {now_jst}</footer>
</body>
</html>"""


# ---- メイン ------------------------------------------------------------

def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "環境変数 ANTHROPIC_API_KEY が設定されていません。\n"
            "GitHub Actions の場合: Settings → Secrets → ANTHROPIC_API_KEY を追加してください。\n"
            "ローカルの場合: export ANTHROPIC_API_KEY=sk-ant-xxxx"
        )

    client = anthropic.Anthropic(api_key=api_key)

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
            print(f"  処理中: {video['title'][:60]}...")

            transcript = get_transcript(video["video_id"])
            if transcript:
                print(f"  トランスクリプト取得: {len(transcript)} 文字")
            else:
                print("  トランスクリプト: なし（タイトル+概要欄のみで要約）")

            try:
                analysis = summarize_video(
                    client,
                    video["title"],
                    video["description"],
                    transcript,
                )
            except Exception as e:
                print(f"  [ERROR] 要約失敗: {e}")
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

            time.sleep(0.5)  # Claude API レート制限対策

    # 3. HTML & JSON を生成・保存
    DOCS_DIR.mkdir(exist_ok=True)
    archive_dir = DOCS_DIR / "archive"
    archive_dir.mkdir(exist_ok=True)

    # アーカイブ: 当日分の HTML / JSON を保存
    archive_html_path = archive_dir / f"{today}.html"
    with open(archive_html_path, "w", encoding="utf-8") as f:
        f.write(generate_html(all_results, today, is_archive=True))

    with open(archive_dir / f"{today}.json", "w", encoding="utf-8") as f:
        json.dump({"date": today, "results": all_results}, f, ensure_ascii=False, indent=2)

    # アーカイブ一覧ページを更新
    all_dates = sorted(
        [f.stem for f in archive_dir.glob("*.html") if f.stem != "index"],
        reverse=True,
    )
    with open(archive_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(generate_archive_index_html(all_dates))

    # メインページ (index.html) を更新
    with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(generate_html(all_results, today))

    # latest.json（後方互換）
    with open(DOCS_DIR / "latest.json", "w", encoding="utf-8") as f:
        json.dump({"date": today, "results": all_results}, f, ensure_ascii=False, indent=2)

    print(f"\n✓ 完了: {len(all_results)} 本の動画を処理しました。")
    print(f"  HTML:    {DOCS_DIR / 'index.html'}")
    print(f"  Archive: {archive_html_path}")


if __name__ == "__main__":
    main()
