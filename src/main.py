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
            results.append({
                "video_id": entry.get("yt_videoid", ""),
                "title":    entry.get("title", ""),
                "url":      entry.get("link", ""),
                "published": published.isoformat(),
                "description": entry.get("summary", ""),
            })
    return results


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

_SENTIMENT_ICON = {"bullish": "📈", "bearish": "📉", "neutral": "➡️"}
_IMPORTANCE_COLOR = {5: "#f59e0b", 4: "#3b82f6", 3: "#10b981", 2: "#64748b", 1: "#334155"}


def _stars(n: int) -> str:
    return "★" * max(1, min(5, n)) + "☆" * (5 - max(1, min(5, n)))


def _render_card(r: dict) -> str:
    a = r.get("analysis", {})
    importance = int(a.get("importance", 1))
    sentiment  = a.get("sentiment", "neutral")
    tickers    = a.get("tickers", [])
    topics     = a.get("topics", [])
    key_points = a.get("key_points", [])
    color      = _IMPORTANCE_COLOR.get(importance, "#334155")
    icon       = _SENTIMENT_ICON.get(sentiment, "➡️")

    ticker_html = "".join(f'<span class="ticker">{t}</span>' for t in tickers)
    topic_html  = "".join(f'<span class="topic">{t}</span>'  for t in topics)
    points_html = "".join(f"<li>{p}</li>" for p in key_points)
    pub_date    = r.get("published", "")[:10]

    return f"""
    <article class="card" style="border-left-color:{color}">
      <div class="card-header">
        <span class="channel">{r['channel_name']}</span>
        <span class="meta">{icon} <span class="stars">{_stars(importance)}</span></span>
      </div>
      <h3><a href="{r['url']}" target="_blank" rel="noopener">{r['title']}</a></h3>
      <div class="tags">{ticker_html}{topic_html}</div>
      <p class="summary">{a.get('summary_ja', '')}</p>
      {"<ul class='points'>" + points_html + "</ul>" if points_html else ""}
      <time class="pub-date">{pub_date}</time>
    </article>"""


def generate_html(results: list[dict], date_str: str) -> str:
    results_sorted = sorted(
        results,
        key=lambda x: int(x.get("analysis", {}).get("importance", 1)),
        reverse=True,
    )
    cards = "".join(_render_card(r) for r in results_sorted)
    if not cards:
        cards = '<p class="empty">本日の新着動画はありません。</p>'

    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>投資YouTube日次ダイジェスト {date_str}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', sans-serif;
      background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 24px 16px 48px;
    }}
    header {{ text-align: center; margin-bottom: 32px; }}
    header h1 {{ font-size: clamp(1.4rem, 4vw, 2rem); color: #38bdf8; margin-bottom: 6px; }}
    header p  {{ color: #94a3b8; font-size: 0.9rem; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      gap: 20px; max-width: 1400px; margin: 0 auto;
    }}
    .card {{
      background: #1e293b; border-radius: 12px; padding: 18px 20px;
      border-left: 4px solid #475569;
      transition: box-shadow 0.2s, transform 0.2s;
    }}
    .card:hover {{ box-shadow: 0 4px 24px rgba(0,0,0,0.4); transform: translateY(-2px); }}
    .card-header {{
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 10px;
    }}
    .channel {{ font-size: 0.72rem; color: #94a3b8; font-weight: 700;
                text-transform: uppercase; letter-spacing: 0.06em; }}
    .meta    {{ display: flex; align-items: center; gap: 6px; font-size: 0.75rem; color: #cbd5e1; }}
    .stars   {{ color: #f59e0b; font-size: 0.7rem; letter-spacing: -1px; }}
    h3       {{ font-size: 0.95rem; line-height: 1.5; margin-bottom: 10px; }}
    h3 a     {{ color: #93c5fd; text-decoration: none; }}
    h3 a:hover {{ color: #38bdf8; text-decoration: underline; }}
    .tags    {{ display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 12px; }}
    .ticker  {{
      background: #1d4ed8; color: #bfdbfe;
      padding: 2px 7px; border-radius: 4px;
      font-size: 0.72rem; font-weight: 700; font-family: monospace;
    }}
    .topic   {{
      background: #134e4a; color: #6ee7b7;
      padding: 2px 7px; border-radius: 4px; font-size: 0.72rem;
    }}
    .summary {{ color: #cbd5e1; font-size: 0.88rem; line-height: 1.7; margin-bottom: 10px; }}
    .points  {{ color: #94a3b8; font-size: 0.83rem; padding-left: 18px; line-height: 1.9; margin-bottom: 10px; }}
    .pub-date {{ display: block; color: #475569; font-size: 0.72rem; }}
    .empty   {{ text-align: center; color: #64748b; padding: 60px; grid-column: 1/-1; font-size: 1.1rem; }}
    footer   {{ text-align: center; color: #334155; font-size: 0.78rem; margin-top: 32px; }}
  </style>
</head>
<body>
  <header>
    <h1>📊 投資YouTube 日次ダイジェスト</h1>
    <p>{date_str} &nbsp;|&nbsp; {len(results)} 本の新着動画</p>
  </header>
  <div class="grid">
    {cards}
  </div>
  <footer>最終更新: {now_jst}</footer>
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

    html_path = DOCS_DIR / "index.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(generate_html(all_results, today))

    json_path = DOCS_DIR / "latest.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"date": today, "results": all_results}, f, ensure_ascii=False, indent=2)

    print(f"\n✓ 完了: {len(all_results)} 本の動画を処理しました。")
    print(f"  HTML: {html_path}")
    print(f"  JSON: {json_path}")


if __name__ == "__main__":
    main()
