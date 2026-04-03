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
import anthropic

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


# ---- トランスクリプト ---------------------------------------------------

# ---- Claude API 要約 ---------------------------------------------------

SUMMARY_PROMPT_TEMPLATE = """\
以下の投資系YouTube動画から、個人投資家にとって重要な情報を抽出してください。

タイトル: {title}

概要欄:
{description}

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
) -> dict:
    """Claude API を使って動画の投資情報を要約する。"""
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        title=title,
        description=description[:2000],
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
    imp_pct     = importance * 20
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
    <a class="watch-btn" href="{r['url']}" target="_blank" rel="noopener">watch ↗</a>
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

    root_path    = "../" if is_archive else ""
    favicon_href = "../favicon.svg" if is_archive else "favicon.svg"
    if is_archive:
        nav_links = ("<a href='index.html' class='nav-link'>all archives</a>"
                     "<a href='../index.html' class='nav-link'>← latest</a>")
    else:
        nav_links = "<a href='archive/index.html' class='nav-link'>archive →</a>"

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
    html {{ scroll-behavior: smooth; }}
    body {{
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px;
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
      height: 52px; gap: 12px;
    }}
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
    .nav-pills {{ display: flex; align-items: center; gap: 6px; flex: 1; justify-content: center; }}
    .stat-pill {{
      font-size: 0.65rem; color: var(--muted);
      background: var(--surface); border: 1px solid var(--border);
      padding: 3px 10px; border-radius: 2px;
      display: flex; align-items: center; gap: 5px;
    }}
    .dot {{ width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }}
    .dot-bull {{ background: var(--brand); }}
    .dot-bear {{ background: var(--red); }}
    .dot-neut {{ background: var(--muted); }}
    nav {{ display: flex; align-items: center; gap: 4px; flex-shrink: 0; }}
    .nav-link {{
      font-size: 0.7rem; color: var(--muted); text-decoration: none;
      padding: 4px 11px; border: 1px solid var(--border); border-radius: 2px;
      font-family: inherit; transition: all 150ms ease;
    }}
    .nav-link:hover {{ color: var(--brand); border-color: rgba(0,255,136,0.3); }}

    /* hero */
    .hero {{
      text-align: center;
      padding: clamp(52px,7vw,88px) 16px clamp(28px,4vw,48px);
    }}
    .hero-prompt {{
      font-size: 0.72rem; color: var(--muted);
      display: flex; align-items: center; justify-content: center; gap: 8px;
      margin-bottom: 22px;
    }}
    .prompt-symbol {{ color: var(--brand); opacity: 0.7; }}
    .prompt-date {{
      color: var(--brand); font-weight: 700;
      border: 1px solid rgba(0,255,136,0.25); padding: 2px 10px;
      border-radius: 2px; font-size: 0.82rem;
    }}
    .hero h1 {{
      font-size: clamp(2rem,5vw,3rem); font-weight: 700;
      letter-spacing: -0.04em; line-height: 1.1; margin-bottom: 10px;
    }}
    .hero h1 .green {{ color: var(--brand); }}
    .hero-tagline {{
      font-size: 0.68rem; color: var(--dim); letter-spacing: 2px; margin-bottom: 40px;
    }}
    .hero-stats {{
      display: flex; justify-content: center; gap: clamp(24px,5vw,72px); flex-wrap: wrap;
    }}
    .h-stat {{ text-align: center; }}
    .h-stat-num {{ font-size: clamp(1.6rem,3.5vw,2.2rem); font-weight: 700; line-height: 1; }}
    .h-stat-num.c-total {{ color: var(--text); }}
    .h-stat-num.c-bull  {{ color: var(--brand); }}
    .h-stat-num.c-bear  {{ color: var(--red); }}
    .h-stat-num.c-neut  {{ color: var(--muted); }}
    .h-stat-label {{ font-size: 0.6rem; color: var(--dim); margin-top: 6px; letter-spacing: 0.12em; }}

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
    .card:hover   {{ transform: translateY(-2px); }}
    .card.sent-bullish {{ border-left: 2px solid var(--brand); }}
    .card.sent-bearish {{ border-left: 2px solid var(--red); }}
    .card.sent-neutral {{ border-left: 2px solid rgba(255,255,255,0.12); }}
    .card.sent-bullish:hover {{ border-color: rgba(0,255,136,0.25); border-left-color: var(--brand); }}
    .card.sent-bearish:hover {{ border-color: rgba(255,68,68,0.25); border-left-color: var(--red); }}

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

    .imp-track {{
      height: 2px; background: var(--border); border-radius: 1px;
      margin: 4px 0 12px; overflow: hidden;
    }}
    .imp-fill {{ height: 100%; border-radius: 1px; background: rgba(255,255,255,0.2); transition: width 500ms ease; }}
    .card.imp-5 .imp-fill {{ background: var(--brand); }}
    .card.imp-4 .imp-fill {{ background: rgba(0,255,136,0.5); }}
    .card.imp-3 .imp-fill {{ background: rgba(255,255,255,0.3); }}

    .summary {{ font-size: 0.81rem; line-height: 1.82; color: rgba(255,255,255,0.75); margin-bottom: 10px; }}

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
      .nav-pills {{ display: none; }}
      .hero h1 {{ font-size: 1.7rem; }}
      .grid {{ grid-template-columns: 1fr; }}
      .filters {{ flex-wrap: nowrap; overflow-x: auto; padding-bottom: 6px; }}
      .filters::-webkit-scrollbar {{ display: none; }}
    }}
  </style>
</head>
<body>

  <header>
    <div class="header-inner">
      <a href="{root_path}index.html" class="ad-logo">
        <span class="ad-logo__cursor"></span>
        <span class="ad-logo__alpha">alpha</span>
        <span class="ad-logo__digest">digest</span>
      </a>
      <div class="nav-pills">
        <div class="stat-pill"><span class="dot dot-bull"></span>{n_bullish} bullish</div>
        <div class="stat-pill"><span class="dot dot-bear"></span>{n_bearish} bearish</div>
        <div class="stat-pill"><span class="dot dot-neut"></span>{n_neutral} neutral</div>
      </div>
      <nav>
        <a href="{root_path}channels.html" class="nav-link">channels</a>
        {nav_links}
      </nav>
    </div>
  </header>

  <section class="hero">
    <div class="hero-prompt">
      <span class="prompt-symbol">></span>
      <span>digest</span>
      <span class="prompt-date">{date_str}</span>
    </div>
    <h1><span class="green">alpha</span>digest</h1>
    <p class="hero-tagline">// top investment voices, distilled</p>
    <div class="hero-stats">
      <div class="h-stat"><div class="h-stat-num c-total">{len(results)}</div><div class="h-stat-label">total</div></div>
      <div class="h-stat"><div class="h-stat-num c-bull">{n_bullish}</div><div class="h-stat-label">bullish</div></div>
      <div class="h-stat"><div class="h-stat-num c-bear">{n_bearish}</div><div class="h-stat-label">bearish</div></div>
      <div class="h-stat"><div class="h-stat-num c-neut">{n_neutral}</div><div class="h-stat-label">neutral</div></div>
    </div>
  </section>

  <div class="filters">
    <div class="filter-segment">
      <button class="filter-btn active" data-filter="all">all</button>
      <button class="filter-btn f-bull" data-filter="bullish">▲ bullish</button>
      <button class="filter-btn f-bear" data-filter="bearish">▼ bearish</button>
      <button class="filter-btn f-neut" data-filter="neutral">● neutral</button>
    </div>
    <div class="filter-segment">
      <button class="filter-btn" data-imp="5">top picks</button>
      <button class="filter-btn" data-imp="4">important+</button>
    </div>
  </div>

  <div class="grid-wrap">
    <div class="grid" id="grid">
      {cards}
      <div id="no-results">// no results matching filters</div>
    </div>
  </div>

  <footer>// alphadigest &nbsp;·&nbsp; {now_jst} &nbsp;·&nbsp; powered by claude ai</footer>

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
    body {{
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px;
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
      max-width: 720px; margin: 0 auto;
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
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <a href="../index.html" class="ad-logo">
        <span class="ad-logo__cursor"></span>
        <span class="ad-logo__alpha">alpha</span>
        <span class="ad-logo__digest">digest</span>
      </a>
      <nav>
        <a href="../channels.html" class="nav-link">channels</a>
        <a href="../index.html" class="nav-link">← latest</a>
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
  <footer>// alphadigest · {now_jst}</footer>
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
    body {{
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px;
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
      max-width: 720px; margin: 0 auto;
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
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <a href="index.html" class="ad-logo">
        <span class="ad-logo__cursor"></span>
        <span class="ad-logo__alpha">alpha</span>
        <span class="ad-logo__digest">digest</span>
      </a>
      <nav>
        <a href="archive/index.html" class="nav-link">archive</a>
        <a href="index.html" class="nav-link">← latest</a>
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
  <footer>// alphadigest · {now_jst}</footer>
</body>
</html>"""


# ---- ティッカーページ ---------------------------------------------------

def generate_ticker_page(ticker: str, mentions: list[dict]) -> str:
    """ティッカーシンボル別ダッシュボードページを生成する。"""
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    mention_rows = "".join(
        f'<a class="mention-row" href="../{m["page_path"]}" >'
        f'<span class="m-date">{m["date"]}</span>'
        f'<span class="m-channel">{m["channel_name"]}</span>'
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
    body {{
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px;
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
      grid-template-columns: 96px 160px 1fr 20px;
      align-items: center; gap: 12px;
      background: var(--surface); border: 1px solid var(--border);
      border-left: 2px solid rgba(0,255,136,0.2);
      border-radius: 2px; padding: 10px 14px;
      text-decoration: none; transition: all 150ms ease;
    }}
    .mention-row:hover {{ border-left-color: var(--brand); transform: translateX(3px); }}
    .m-date {{ font-size: 0.65rem; color: var(--dim); flex-shrink: 0; }}
    .m-channel {{ font-size: 0.65rem; color: var(--brand); opacity: 0.7; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .m-title {{ font-size: 0.78rem; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .m-arrow {{ color: var(--brand); font-size: 0.72rem; opacity: 0.6; }}
    .no-mentions {{ color: var(--dim); font-size: 0.78rem; padding: 20px 0; }}
    @media (max-width: 640px) {{
      .mention-row {{ grid-template-columns: 80px 1fr 16px; }}
      .m-channel {{ display: none; }}
    }}
    footer {{ text-align: center; padding: 20px; border-top: 1px solid var(--border); font-size: 0.62rem; color: var(--dim); }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <a href="../index.html" class="ad-logo">
        <span class="ad-logo__cursor"></span>
        <span class="ad-logo__alpha">alpha</span>
        <span class="ad-logo__digest">digest</span>
      </a>
      <nav>
        <a href="../index.html" class="nav-link">← latest</a>
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
  <footer>// alphadigest · {now_jst}</footer>
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

    # チャンネルページを生成
    with open(DOCS_DIR / "channels.html", "w", encoding="utf-8") as f:
        f.write(generate_channels_html(channels))

    # メインページ (index.html) を更新
    with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(generate_html(all_results, today))

    # latest.json（後方互換）
    with open(DOCS_DIR / "latest.json", "w", encoding="utf-8") as f:
        json.dump({"date": today, "results": all_results}, f, ensure_ascii=False, indent=2)

    # ティッカーページ生成: 全アーカイブJSONを走査してティッカー別に集計
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
                })

    ticker_dir = DOCS_DIR / "ticker"
    ticker_dir.mkdir(exist_ok=True)
    for ticker, mentions in ticker_mentions.items():
        mentions_sorted = sorted(mentions, key=lambda x: x["date"], reverse=True)
        with open(ticker_dir / f"{ticker}.html", "w", encoding="utf-8") as f:
            f.write(generate_ticker_page(ticker, mentions_sorted))
    print(f"  Tickers: {len(ticker_mentions)} ページ生成")

    print(f"\n✓ 完了: {len(all_results)} 本の動画を処理しました。")
    print(f"  HTML:    {DOCS_DIR / 'index.html'}")
    print(f"  Archive: {archive_html_path}")


if __name__ == "__main__":
    main()
