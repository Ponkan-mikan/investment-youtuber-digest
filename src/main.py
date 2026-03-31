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
    try:
        # youtube-transcript-api v1.x: インスタンスメソッドに変更
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id, languages=["en", "en-US", "en-GB"])
        text = " ".join(s.get("text", "") for s in transcript)
        return text[:TRANSCRIPT_MAX_CHARS]
    except (NoTranscriptFound, TranscriptsDisabled):
        return None
    except Exception as e:
        print(f"  [WARN] トランスクリプト取得失敗 {video_id}: {e}")
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
_IMP_BAR    = lambda n: "▮" * n + "▯" * (5 - n)


def _render_card(r: dict) -> str:
    a          = r.get("analysis", {})
    importance = max(1, min(5, int(a.get("importance", 1))))
    sentiment  = a.get("sentiment", "neutral")
    tickers    = a.get("tickers", [])
    topics     = a.get("topics", [])
    key_points = a.get("key_points", [])
    pub_date   = format_pub_datetime(r.get("published", ""))

    sent_label   = _SENT_LABEL.get(sentiment, sentiment)
    imp_bar      = _IMP_BAR(importance)
    ticker_html  = "".join(f'<span class="ticker">{t}</span>' for t in tickers)
    topic_html   = "".join(f'<span class="topic">{t}</span>'  for t in topics)
    points_html  = "".join(f"<li>{p}</li>" for p in key_points)
    points_block = f'<ul class="points">{points_html}</ul>' if points_html else ""
    tags_block   = f'<div class="tags">{ticker_html}{topic_html}</div>' if (tickers or topics) else ""

    return f"""<article class="card imp-{importance} sent-{sentiment}"
  data-sentiment="{sentiment}" data-importance="{importance}">
  <div class="card-top">
    <span class="channel-name">{r['channel_name']}</span>
    <div class="badges">
      <span class="badge-sent {sentiment}">{sent_label}</span>
      <span class="badge-imp">{imp_bar}</span>
    </div>
  </div>
  <h3><a href="{r['url']}" target="_blank" rel="noopener">{r['title']}</a></h3>
  {tags_block}
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

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Investment Digest — {date_str}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:        #04040a;
      --bg2:       #0c0c18;
      --surface:   rgba(255,255,255,0.04);
      --surface2:  rgba(255,255,255,0.07);
      --border:    rgba(255,255,255,0.08);
      --text:      #e8eaf0;
      --muted:     #6b7280;
      --bullish:   #10b981;
      --bearish:   #ef4444;
      --neutral:   #6366f1;
      --gold:      #f59e0b;
      --blue:      #3b82f6;
      --ticker-bg: #001a0a;
      --ticker-fg: #00ff88;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    html {{ scroll-behavior: smooth; }}

    body {{
      font-family: 'Inter', -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      background-image:
        radial-gradient(ellipse 80% 50% at 50% -10%, rgba(99,102,241,0.12) 0%, transparent 60%),
        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(16,185,129,0.06) 0%, transparent 50%);
    }}

    /* ── Scrollbar ── */
    ::-webkit-scrollbar {{ width: 6px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: #2d2d4e; border-radius: 3px; }}

    /* ── Header ── */
    header {{
      position: sticky; top: 0; z-index: 100;
      background: rgba(4,4,10,0.85);
      backdrop-filter: blur(20px);
      border-bottom: 1px solid var(--border);
      padding: 0 clamp(16px, 4vw, 48px);
    }}
    .header-inner {{
      max-width: 1440px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between;
      height: 64px; gap: 16px;
    }}
    .logo {{
      display: flex; align-items: center; gap: 10px;
      font-size: 1.05rem; font-weight: 700; letter-spacing: -0.02em;
      white-space: nowrap;
    }}
    .logo-icon {{
      width: 32px; height: 32px; border-radius: 8px;
      background: linear-gradient(135deg, #6366f1 0%, #10b981 100%);
      display: flex; align-items: center; justify-content: center;
      font-size: 1rem;
    }}
    .header-meta {{
      display: flex; align-items: center; gap: 20px;
      font-size: 0.78rem; color: var(--muted);
    }}
    .stat-pill {{
      display: flex; align-items: center; gap: 5px;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 20px; padding: 4px 12px;
      font-size: 0.75rem;
    }}
    .stat-pill .dot {{ width: 6px; height: 6px; border-radius: 50%; }}
    .dot-bullish {{ background: var(--bullish); box-shadow: 0 0 6px var(--bullish); }}
    .dot-bearish {{ background: var(--bearish); box-shadow: 0 0 6px var(--bearish); }}
    .dot-neutral {{ background: var(--neutral); box-shadow: 0 0 6px var(--neutral); }}

    /* ── Hero ── */
    .hero {{
      text-align: center;
      padding: clamp(40px, 6vw, 80px) 16px clamp(24px, 4vw, 48px);
    }}
    .hero-date {{
      display: inline-block;
      font-size: 0.72rem; font-weight: 600; letter-spacing: 0.15em;
      text-transform: uppercase; color: var(--neutral);
      background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.25);
      border-radius: 20px; padding: 4px 14px; margin-bottom: 20px;
    }}
    .hero h1 {{
      font-size: clamp(2rem, 5vw, 3.2rem);
      font-weight: 700; letter-spacing: -0.03em; line-height: 1.1;
      background: linear-gradient(135deg, #fff 0%, #a5b4fc 50%, #34d399 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      background-clip: text; margin-bottom: 16px;
    }}
    .hero-sub {{
      font-size: 0.9rem; color: var(--muted);
    }}
    .hero-stats {{
      display: flex; justify-content: center; gap: clamp(16px,3vw,40px);
      margin-top: 28px; flex-wrap: wrap;
    }}
    .h-stat {{ text-align: center; }}
    .h-stat-num {{
      font-size: clamp(1.6rem, 4vw, 2.2rem); font-weight: 700;
      letter-spacing: -0.04em; line-height: 1;
    }}
    .h-stat-num.c-total  {{ color: #fff; }}
    .h-stat-num.c-bull   {{ color: var(--bullish); }}
    .h-stat-num.c-bear   {{ color: var(--bearish); }}
    .h-stat-num.c-neut   {{ color: var(--neutral); }}
    .h-stat-label {{ font-size: 0.72rem; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.08em; }}

    /* ── Filters ── */
    .filters {{
      max-width: 1440px; margin: 0 auto 28px;
      padding: 0 clamp(16px,4vw,48px);
      display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
    }}
    .filter-label {{ font-size: 0.72rem; color: var(--muted); margin-right: 4px; text-transform: uppercase; letter-spacing: 0.08em; }}
    .filter-btn {{
      background: var(--surface); border: 1px solid var(--border);
      color: var(--muted); border-radius: 20px; padding: 6px 16px;
      font-size: 0.78rem; font-family: 'Inter', sans-serif; cursor: pointer;
      transition: all 0.18s ease;
    }}
    .filter-btn:hover {{ background: var(--surface2); color: var(--text); border-color: rgba(255,255,255,0.16); }}
    .filter-btn.active {{ color: var(--text); border-color: rgba(255,255,255,0.3); background: var(--surface2); }}
    .filter-btn.f-bullish.active {{ border-color: var(--bullish); color: var(--bullish); background: rgba(16,185,129,0.1); }}
    .filter-btn.f-bearish.active {{ border-color: var(--bearish); color: var(--bearish); background: rgba(239,68,68,0.1); }}
    .filter-btn.f-neutral.active {{ border-color: var(--neutral); color: var(--neutral); background: rgba(99,102,241,0.1); }}
    .filter-divider {{ width: 1px; height: 20px; background: var(--border); margin: 0 4px; }}

    /* ── Grid ── */
    .grid-wrap {{ max-width: 1440px; margin: 0 auto; padding: 0 clamp(16px,4vw,48px) 80px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
      gap: 16px;
    }}
    #no-results {{
      display: none; text-align: center; color: var(--muted);
      padding: 80px 20px; grid-column: 1/-1; font-size: 1rem;
    }}
    .empty {{ text-align: center; color: var(--muted); padding: 80px 20px; font-size: 1rem; }}

    /* ── Cards ── */
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px; padding: 20px 22px;
      position: relative; overflow: hidden;
      transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.22s ease;
      animation: fadeUp 0.4s ease both;
    }}
    .card::before {{
      content: ''; position: absolute; inset: 0;
      border-radius: inherit; opacity: 0;
      transition: opacity 0.22s ease;
      pointer-events: none;
    }}
    .card:hover {{ transform: translateY(-4px); }}

    /* Importance glow */
    .card.imp-5 {{ border-color: rgba(245,158,11,0.3); }}
    .card.imp-5:hover {{ box-shadow: 0 0 32px rgba(245,158,11,0.2); border-color: rgba(245,158,11,0.6); }}
    .card.imp-4 {{ border-color: rgba(59,130,246,0.25); }}
    .card.imp-4:hover {{ box-shadow: 0 0 24px rgba(59,130,246,0.18); border-color: rgba(59,130,246,0.5); }}
    .card.imp-3:hover {{ box-shadow: 0 0 16px rgba(255,255,255,0.05); }}

    /* Importance top bar */
    .card.imp-5::after {{ content:''; position:absolute; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg,#f59e0b,#fbbf24); border-radius:16px 16px 0 0; }}
    .card.imp-4::after {{ content:''; position:absolute; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg,#3b82f6,#60a5fa); border-radius:16px 16px 0 0; }}

    .card-top {{
      display: flex; justify-content: space-between; align-items: flex-start;
      gap: 8px; margin-bottom: 12px;
    }}
    .channel-name {{
      font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.1em; color: var(--muted);
    }}
    .badges {{ display: flex; align-items: center; gap: 6px; flex-shrink: 0; }}

    .badge-sent {{
      font-size: 0.68rem; font-weight: 600; letter-spacing: 0.04em;
      border-radius: 20px; padding: 2px 9px; border: 1px solid;
    }}
    .badge-sent.bullish {{ color: var(--bullish); border-color: rgba(16,185,129,0.4); background: rgba(16,185,129,0.1); }}
    .badge-sent.bearish {{ color: var(--bearish); border-color: rgba(239,68,68,0.4);  background: rgba(239,68,68,0.1); }}
    .badge-sent.neutral {{ color: var(--neutral); border-color: rgba(99,102,241,0.4); background: rgba(99,102,241,0.1); }}

    .badge-imp {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.62rem; color: var(--gold); letter-spacing: 1px;
    }}
    .card.imp-4 .badge-imp {{ color: var(--blue); }}
    .card.imp-3 .badge-imp {{ color: #6b7280; }}
    .card.imp-2 .badge-imp, .card.imp-1 .badge-imp {{ color: #374151; }}

    h3 {{
      font-size: 0.95rem; font-weight: 600; line-height: 1.5;
      margin-bottom: 12px;
    }}
    h3 a {{ color: var(--text); text-decoration: none; }}
    h3 a:hover {{ color: #a5b4fc; }}

    /* Tags */
    .tags {{ display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 14px; }}
    .ticker {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.7rem; font-weight: 700;
      background: var(--ticker-bg); color: var(--ticker-fg);
      border: 1px solid rgba(0,255,136,0.25);
      padding: 2px 8px; border-radius: 5px; letter-spacing: 0.05em;
    }}
    .topic {{
      font-size: 0.7rem;
      background: rgba(99,102,241,0.1); color: #a5b4fc;
      border: 1px solid rgba(99,102,241,0.2);
      padding: 2px 8px; border-radius: 5px;
    }}

    /* Summary & points */
    .summary {{
      font-size: 0.875rem; line-height: 1.75; color: #9ca3af;
      margin-bottom: 12px;
    }}
    .points {{
      list-style: none; padding: 0; margin-bottom: 14px;
    }}
    .points li {{
      font-size: 0.82rem; color: #6b7280; line-height: 1.7;
      padding-left: 16px; position: relative;
    }}
    .points li::before {{
      content: '›'; position: absolute; left: 0; color: #4b5563;
    }}

    /* Card footer */
    .card-footer {{
      display: flex; justify-content: space-between; align-items: center;
      padding-top: 12px; border-top: 1px solid var(--border);
      margin-top: 4px;
    }}
    .card-footer time {{ font-size: 0.7rem; color: var(--muted); }}
    .watch-btn {{
      font-size: 0.72rem; font-weight: 600;
      color: var(--muted); text-decoration: none;
      padding: 4px 10px; border-radius: 8px;
      border: 1px solid var(--border);
      transition: all 0.15s ease;
    }}
    .watch-btn:hover {{
      color: var(--text); border-color: rgba(255,255,255,0.2);
      background: var(--surface2);
    }}

    /* ── Footer ── */
    footer {{
      text-align: center; padding: 24px 16px;
      border-top: 1px solid var(--border);
      font-size: 0.75rem; color: var(--muted);
    }}

    /* ── Animations ── */
    @keyframes fadeUp {{
      from {{ opacity: 0; transform: translateY(16px); }}
      to   {{ opacity: 1; transform: translateY(0); }}
    }}
    .card:nth-child(1)  {{ animation-delay: 0.03s; }}
    .card:nth-child(2)  {{ animation-delay: 0.06s; }}
    .card:nth-child(3)  {{ animation-delay: 0.09s; }}
    .card:nth-child(4)  {{ animation-delay: 0.12s; }}
    .card:nth-child(5)  {{ animation-delay: 0.15s; }}
    .card:nth-child(6)  {{ animation-delay: 0.18s; }}
    .card:nth-child(7)  {{ animation-delay: 0.21s; }}
    .card:nth-child(8)  {{ animation-delay: 0.24s; }}
    .card:nth-child(9)  {{ animation-delay: 0.27s; }}
    .card:nth-child(10) {{ animation-delay: 0.30s; }}

    .card.hidden {{ display: none !important; }}

    /* ── Nav links ── */
    .nav-link {{
      font-size: 0.78rem; font-weight: 500; color: var(--muted);
      text-decoration: none; padding: 5px 12px;
      border: 1px solid var(--border); border-radius: 20px;
      transition: all 0.15s ease;
    }}
    .nav-link:hover {{ color: var(--text); border-color: rgba(255,255,255,0.2); background: var(--surface2); }}

    /* ── Responsive ── */
    @media (max-width: 600px) {{
      .header-meta {{ display: none; }}
      .hero h1 {{ font-size: 1.8rem; }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>

  <!-- Header -->
  <header>
    <div class="header-inner">
      <div class="logo">
        <div class="logo-icon">📊</div>
        Investment Digest
      </div>
      <div class="header-meta">
        <div class="stat-pill"><span class="dot dot-bullish"></span>{n_bullish} Bullish</div>
        <div class="stat-pill"><span class="dot dot-bearish"></span>{n_bearish} Bearish</div>
        <div class="stat-pill"><span class="dot dot-neutral"></span>{n_neutral} Neutral</div>
        <span>{now_jst}</span>
        {"<a href='../index.html' class='nav-link'>← Latest</a><a href='index.html' class='nav-link'>All Archives</a>" if is_archive else "<a href='archive/index.html' class='nav-link'>Archive →</a>"}
      </div>
    </div>
  </header>

  <!-- Hero -->
  <section class="hero">
    <div class="hero-date">{date_str}</div>
    <h1>Investment<br>YouTube Digest</h1>
    <p class="hero-sub">米国投資系YouTubeチャンネル 新着動画まとめ</p>
    <div class="hero-stats">
      <div class="h-stat"><div class="h-stat-num c-total">{len(results)}</div><div class="h-stat-label">Total Videos</div></div>
      <div class="h-stat"><div class="h-stat-num c-bull">{n_bullish}</div><div class="h-stat-label">Bullish</div></div>
      <div class="h-stat"><div class="h-stat-num c-bear">{n_bearish}</div><div class="h-stat-label">Bearish</div></div>
      <div class="h-stat"><div class="h-stat-num c-neut">{n_neutral}</div><div class="h-stat-label">Neutral</div></div>
    </div>
  </section>

  <!-- Filters -->
  <div class="filters">
    <span class="filter-label">Filter</span>
    <button class="filter-btn active" data-filter="all">All</button>
    <button class="filter-btn f-bullish" data-filter="bullish">▲ Bullish</button>
    <button class="filter-btn f-bearish" data-filter="bearish">▼ Bearish</button>
    <button class="filter-btn f-neutral" data-filter="neutral">● Neutral</button>
    <div class="filter-divider"></div>
    <button class="filter-btn" data-imp="5">★★★★★</button>
    <button class="filter-btn" data-imp="4">★★★★+</button>
  </div>

  <!-- Grid -->
  <div class="grid-wrap">
    <div class="grid" id="grid">
      {cards}
      <div id="no-results">条件に一致する動画がありません</div>
    </div>
  </div>

  <footer>
    Investment Digest &nbsp;·&nbsp; {now_jst} &nbsp;·&nbsp; Powered by Claude AI
  </footer>

  <script>
    const cards = document.querySelectorAll('.card');
    let activeSentiment = 'all';
    let activeImp = 0;

    function applyFilters() {{
      let visible = 0;
      cards.forEach(c => {{
        const s = c.dataset.sentiment;
        const i = parseInt(c.dataset.importance);
        const matchS = activeSentiment === 'all' || s === activeSentiment;
        const matchI = activeImp === 0 || i >= activeImp;
        if (matchS && matchI) {{ c.classList.remove('hidden'); visible++; }}
        else {{ c.classList.add('hidden'); }}
      }});
      document.getElementById('no-results').style.display = visible === 0 ? 'block' : 'none';
    }}

    document.querySelectorAll('[data-filter]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('[data-filter]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        activeSentiment = btn.dataset.filter;
        applyFilters();
      }});
    }});

    document.querySelectorAll('[data-imp]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const val = parseInt(btn.dataset.imp);
        if (activeImp === val) {{
          activeImp = 0;
          btn.classList.remove('active');
        }} else {{
          document.querySelectorAll('[data-imp]').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          activeImp = val;
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
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Inter', sans-serif;
      background: #04040a; color: #e8eaf0; min-height: 100vh;
      background-image: radial-gradient(ellipse 80% 50% at 50% -10%, rgba(99,102,241,0.12) 0%, transparent 60%);
    }}
    header {{
      position: sticky; top: 0; z-index: 100;
      background: rgba(4,4,10,0.85); backdrop-filter: blur(20px);
      border-bottom: 1px solid rgba(255,255,255,0.08);
      padding: 0 clamp(16px,4vw,48px);
    }}
    .header-inner {{
      max-width: 800px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between;
      height: 64px;
    }}
    .logo {{ display: flex; align-items: center; gap: 10px; font-size: 1.05rem; font-weight: 700; }}
    .logo-icon {{ width:32px;height:32px;border-radius:8px;background:linear-gradient(135deg,#6366f1,#10b981);display:flex;align-items:center;justify-content:center;font-size:1rem; }}
    .nav-link {{
      font-size: 0.78rem; font-weight: 500; color: #6b7280;
      text-decoration: none; padding: 5px 12px;
      border: 1px solid rgba(255,255,255,0.08); border-radius: 20px;
      transition: all 0.15s ease;
    }}
    .nav-link:hover {{ color: #e8eaf0; border-color: rgba(255,255,255,0.2); background: rgba(255,255,255,0.07); }}
    main {{ max-width: 800px; margin: 0 auto; padding: clamp(40px,6vw,80px) clamp(16px,4vw,48px) 80px; }}
    h1 {{
      font-size: clamp(1.8rem,4vw,2.6rem); font-weight: 700; letter-spacing: -0.03em;
      background: linear-gradient(135deg, #fff 0%, #a5b4fc 50%, #34d399 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      background-clip: text; margin-bottom: 8px;
    }}
    .subtitle {{ color: #6b7280; font-size: 0.9rem; margin-bottom: 40px; }}
    .archive-list {{ display: flex; flex-direction: column; gap: 8px; }}
    .archive-row {{
      display: flex; justify-content: space-between; align-items: center;
      background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px; padding: 16px 20px; text-decoration: none;
      transition: all 0.18s ease;
    }}
    .archive-row:hover {{
      background: rgba(255,255,255,0.07); border-color: rgba(99,102,241,0.4);
      transform: translateX(4px);
    }}
    .arc-date {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 1rem; font-weight: 600; color: #e8eaf0;
    }}
    .arc-arrow {{ color: #6366f1; font-size: 1.1rem; }}
    .arc-empty {{ color: #6b7280; text-align: center; padding: 40px; }}
    footer {{ text-align: center; padding: 24px; border-top: 1px solid rgba(255,255,255,0.08); font-size: 0.75rem; color: #374151; }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div class="logo"><div class="logo-icon">📊</div>Investment Digest</div>
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
