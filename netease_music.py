#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网易云音乐爬虫脚本（纯 Python 标准库，无需 pip 安装任何依赖）

功能：
  1. search    按关键词搜索歌曲
  2. download  下载歌曲 MP3（免费歌曲可匿名下载，VIP/受限歌曲需登录 Cookie）
  3. lyric     获取歌词（含翻译歌词）
  4. comments  分页抓取歌曲评论
  5. playlist  按歌单 ID 导出歌单内的歌曲列表

用法示例：
  python netease_music.py search 晴天
  python netease_music.py download 186016 -o music
  python netease_music.py lyric 186016
  python netease_music.py comments 186016 --pages 2
  python netease_music.py playlist 3778678

登录 Cookie（部分 VIP 歌曲下载需要）：
  Windows PowerShell：
      $env:NCM_COOKIE = "MUSIC_U=xxx; __csrf=xxx"
  Linux / macOS：
      export NCM_COOKIE="MUSIC_U=xxx; __csrf=xxx"
  也可以在命令行用 --cookie "MUSIC_U=xxx; __csrf=xxx" 传入。

注意：本脚本仅供个人学习与研究，请遵守网易云音乐服务条款及相关版权法规，
      不要用于商业用途或大规模抓取。
"""

import argparse
import base64
import gzip
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://music.163.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _safe_stdout():
    """Windows 控制台中文输出兼容。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


class Http:
    """基于 urllib 的简易 HTTP 客户端，自动带网易云需要的请求头。"""

    def __init__(self, cookie=""):
        self.cookie = cookie

    def request(self, url, params=None, extra_headers=None):
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": UA,
            "Referer": BASE + "/",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, headers=headers)
        return urllib.request.urlopen(req, timeout=15)

    def post_form(self, url, data, extra_headers=None):
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers = {
            "User-Agent": UA,
            "Referer": BASE + "/",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": (self.cookie + "; " if self.cookie else "") + "os=pc",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=body, headers=headers)
        return urllib.request.urlopen(req, timeout=15)

    def get_json(self, url, params=None, retries=3):
        last_err = None
        for attempt in range(retries):
            try:
                with self.request(url, params) as resp:
                    raw = resp.read()
                    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    data = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(data, dict) and data.get("code") not in (None, 200):
                    raise RuntimeError(
                        f"接口返回错误码 {data.get('code')}: {data.get('message') or ''}".strip()
                    )
                return data
            except Exception as exc:
                last_err = exc
                if attempt < retries - 1:
                    time.sleep(1.2 * (attempt + 1))
        raise RuntimeError(f"请求失败: {url} -> {last_err}")


# ---------------------------------------------------------------------------
# weapi 加密（纯标准库实现 AES-128-CBC 与 RSA，用于获取真实播放地址）
# ---------------------------------------------------------------------------

_WAPI_NONCE = b"0CoJUm6Qyw8W8jud"
_WAPI_IV = b"0102030405060708"
_WAPI_PUBKEY = "010001"
_WAPI_MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b7251"
    "52b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ec"
    "bda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d81"
    "3cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)

_AES_SBOX = None


def _rotl8(x, n):
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _make_sbox():
    sbox = [0] * 256
    p = q = 1
    while True:
        p ^= p << 1
        if p & 0x100:
            p ^= 0x11B
        p &= 0xFF
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        if q & 0x80:
            q ^= 0x09
        q &= 0xFF
        x = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4)
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


def _get_sbox():
    global _AES_SBOX
    if _AES_SBOX is None:
        _AES_SBOX = _make_sbox()
    return _AES_SBOX


def _xtime(x):
    return ((x << 1) ^ (0x1B if x & 0x80 else 0)) & 0xFF


def _expand_key(key, sbox):
    w = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = w[i - 1][:]
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [sbox[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return w


def _encrypt_block(block, w, sbox):
    state = [[block[c * 4 + r] for r in range(4)] for c in range(4)]

    def add_rk(rnd):
        for c in range(4):
            word = w[rnd * 4 + c]
            for r in range(4):
                state[c][r] ^= word[r]

    add_rk(0)
    for rnd in range(1, 11):
        for c in range(4):
            for r in range(4):
                state[c][r] = sbox[state[c][r]]
        state = [[state[(c + r) % 4][r] for r in range(4)] for c in range(4)]
        if rnd < 10:
            for c in range(4):
                b = state[c]
                a, bb, cc, d = b
                state[c] = [
                    _xtime(a) ^ (_xtime(bb) ^ bb) ^ cc ^ d,
                    a ^ _xtime(bb) ^ (_xtime(cc) ^ cc) ^ d,
                    a ^ bb ^ _xtime(cc) ^ (_xtime(d) ^ d),
                    (_xtime(a) ^ a) ^ bb ^ cc ^ _xtime(d),
                ]
        add_rk(rnd)
    return bytes(state[c][r] for c in range(4) for r in range(4))


def aes_cbc_encrypt(data, key, iv):
    sbox = _get_sbox()
    w = _expand_key(key, sbox)
    prev = list(iv)
    out = bytearray()
    for i in range(0, len(data), 16):
        blk = bytes(data[i + j] ^ prev[j] for j in range(16))
        enc = _encrypt_block(blk, w, sbox)
        out.extend(enc)
        prev = list(enc)
    return bytes(out)


def aes_encrypt(text, key):
    pad = 16 - len(text) % 16
    text = text + bytes([pad]) * pad
    return base64.b64encode(aes_cbc_encrypt(text, key, _WAPI_IV)).decode()


def rsa_encrypt(text):
    rev = text[::-1]
    m = int(rev.encode("utf-8").hex(), 16)
    return format(pow(m, int(_WAPI_PUBKEY, 16), int(_WAPI_MODULUS, 16)), "x").zfill(256)


def create_secret_key(size=16):
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(size))


def weapi_params(payload):
    data = json.dumps(payload).encode("utf-8")
    secret = create_secret_key()
    params = aes_encrypt(aes_encrypt(data, _WAPI_NONCE).encode(), secret.encode())
    return {"params": params, "encSecKey": rsa_encrypt(secret)}


def csrf_token(cookie):
    m = re.search(r"__csrf=([0-9a-zA-Z]+)", cookie or "")
    return m.group(1) if m else ""


def weapi_post(http, path, payload):
    form = weapi_params(payload)
    with http.post_form(f"{BASE}/weapi{path}?csrf_token=", form) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))


def song_artists(song):
    artists = song.get("artists") or song.get("ar") or []
    return "/".join(a.get("name", "") for a in artists)


def song_album(song):
    album = song.get("album") or song.get("al") or {}
    return album.get("name", "")


def song_duration(song):
    ms = song.get("duration") or song.get("dt") or 0
    sec = int(ms) // 1000
    return f"{sec // 60}:{sec % 60:02d}"


def format_song(song, index=None):
    parts = [song.get("name", "未知")]
    artists = song_artists(song)
    if artists:
        parts.append(artists)
    album = song_album(song)
    if album:
        parts.append(album)
    parts.append(song_duration(song))
    line = " - ".join(parts)
    if index is not None:
        line = f"{index:>3}. {line}"
    return f"{line} | id={song.get('id')}"


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    return name or "unknown"


def search_songs(http, keyword, limit=20, offset=0):
    data = http.get_json(
        f"{BASE}/api/search/get/web",
        {"s": keyword, "type": 1, "limit": limit, "offset": offset},
    )
    result = data.get("result") or {}
    return result.get("songs") or [], result.get("songCount", 0)


def get_song_detail(http, song_id):
    data = http.get_json(f"{BASE}/api/song/detail", {"ids": f"[{song_id}]"})
    songs = data.get("songs") or []
    return songs[0] if songs else None


def get_song_url(http, song_id, br=320000):
    level = {96000: "standard", 128000: "standard", 192000: "higher", 320000: "exhigh"}.get(
        br, "exhigh"
    )
    # 优先 weapi v1：目标音质失败时自动降级到 standard（128k）
    for lv in dict.fromkeys([level, "standard"]):
        try:
            data = weapi_post(
                http,
                "/song/enhance/player/url/v1",
                {
                    "ids": f"[{song_id}]",
                    "level": lv,
                    "encodeType": "mp3",
                    "csrf_token": csrf_token(http.cookie),
                },
            )
            items = data.get("data") or []
            if items and items[0].get("url"):
                return items[0]["url"], items[0].get("br") or 0
        except Exception:
            continue
    # 老接口兜底（部分网络环境下仍可用）
    try:
        data = http.get_json(
            f"{BASE}/api/song/enhance/player/url",
            {"ids": f"[{song_id}]", "br": br},
        )
        items = data.get("data") or []
        if items and items[0].get("url"):
            return items[0]["url"], items[0].get("br") or 0
    except Exception:
        pass
    return None, 0


def get_lyric(http, song_id):
    data = http.get_json(
        f"{BASE}/api/song/lyric",
        {"id": song_id, "lv": -1, "kv": -1, "tv": -1},
    )
    lrc = (data.get("lrc") or {}).get("lyric") or ""
    tly = (data.get("tlyric") or {}).get("lyric") or ""
    return lrc, tly


def get_comments(http, song_id, limit=20, offset=0):
    rid = f"R_SO_4_{song_id}"
    return http.get_json(
        f"{BASE}/api/v1/resource/comments/{rid}",
        {"rid": rid, "limit": limit, "offset": offset},
    )


def get_playlist(http, playlist_id):
    try:
        data = http.get_json(f"{BASE}/api/v6/playlist/detail", {"id": playlist_id})
        playlist = data.get("playlist") or {}
        tracks, name = playlist.get("tracks") or [], playlist.get("name", "")
        if tracks or name:
            return tracks, name
    except Exception:
        pass
    data = http.get_json(f"{BASE}/api/playlist/detail", {"id": playlist_id})
    result = data.get("result") or {}
    return result.get("tracks") or [], result.get("name", "")


def download_song(http, song, out_dir, br=320000, overwrite=False):
    filename = sanitize_filename(f"{song.get('name', 'unknown')} - {song_artists(song)}.mp3")
    out = Path(out_dir) / filename
    if out.exists() and not overwrite:
        print(f"已存在: {out}（加 --overwrite 可覆盖）")
        return True

    url, real_br = get_song_url(http, song.get("id"), br)
    if not url:
        raise RuntimeError(
            "未能获取播放地址：歌曲可能受版权/VIP 限制，或当前网络地区不可播放。"
            "可提供登录 Cookie（--cookie 或 NCM_COOKIE 环境变量）后再试。"
        )
    if real_br:
        print(f"获取到音质: {real_br // 1000}kbps")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".mp3.part")
    try:
        with http.request(url, extra_headers={"Accept-Encoding": "identity"}) as resp, open(tmp, "wb") as f:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            head = resp.read(3)
            if not _looks_like_audio(head, content_type):
                raise RuntimeError("返回内容不是音频（可能被反爬拦截），请稍后重试或提供登录 Cookie")
            f.write(head)
            total = int(resp.headers.get("Content-Length") or 0)
            done = len(head)
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 / total
                    print(
                        f"\r下载中 {done / 1048576:.1f}/{total / 1048576:.1f} MB ({pct:.1f}%)",
                        end="",
                        flush=True,
                    )
                else:
                    print(f"\r下载中 {done / 1048576:.1f} MB", end="", flush=True)
        print()
        tmp.replace(out)
        br_text = f"{real_br // 1000}kbps" if real_br else "未知码率"
        print(f"完成: {out}（{done / 1048576:.1f} MB，{br_text}）")
        return True
    except Exception as exc:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f"下载失败: {exc}")


def _looks_like_audio(head, content_type):
    if head[:3] == b"ID3":
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    return "audio/" in content_type or "octet-stream" in content_type


def print_comment(comment, index=None):
    user = (comment.get("user") or {}).get("nickname", "匿名")
    likes = comment.get("likedCount", 0)
    ts = comment.get("time", 0) or 0
    timestr = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000)) if ts else ""
    prefix = f"[{index}] " if index else ""
    print(f"{prefix}{user}（赞 {likes}）{timestr}")
    print(f"    {comment.get('content', '')}")


def cmd_search(args):
    http = Http(cookie=args.cookie)
    songs, count = search_songs(http, args.keyword, args.limit, args.offset)
    if args.json:
        print(json.dumps({"songCount": count, "songs": songs}, ensure_ascii=False, indent=2))
        return 0
    if not songs:
        print("未找到相关歌曲")
        return 0
    end = args.offset + len(songs)
    print(f"共 {count} 条结果，当前显示第 {args.offset + 1}-{end} 条：")
    for i, song in enumerate(songs, args.offset + 1):
        print(format_song(song, i))
    return 0


def cmd_download(args):
    http = Http(cookie=args.cookie)
    song = get_song_detail(http, args.song_id)
    if not song:
        print(f"未找到歌曲 ID {args.song_id}")
        return 1
    print(f"歌曲：{song.get('name')} - {song_artists(song)}")
    download_song(http, song, args.outdir, args.br, args.overwrite)
    return 0


def cmd_lyric(args):
    http = Http(cookie=args.cookie)
    lrc, tly = get_lyric(http, args.song_id)
    if args.json:
        print(json.dumps({"lrc": lrc, "tlyric": tly}, ensure_ascii=False, indent=2))
        return 0
    if not lrc:
        print("该歌曲暂无歌词")
        return 0
    text = lrc
    if tly:
        text += "\n\n[翻译]\n" + tly
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"歌词已保存: {args.out}")
    else:
        print(text)
    return 0


def cmd_comments(args):
    http = Http(cookie=args.cookie)
    all_comments, hot_comments, total = [], [], 0
    for page in range(1, args.pages + 1):
        offset = args.offset + (page - 1) * args.limit
        data = get_comments(http, args.song_id, args.limit, offset)
        total = data.get("total", total)
        comments = data.get("comments") or []
        if page == 1:
            hot_comments = data.get("hotComments") or []
        if not comments:
            break
        all_comments.extend(comments)
        if args.json:
            continue
        print(f"--- 第 {page} 页（本页 {len(comments)} 条，共 {total} 条）---")
        for i, c in enumerate(comments, 1):
            print_comment(c, (page - 1) * args.limit + i)
        if page < args.pages:
            time.sleep(0.5)
    if args.json:
        print(
            json.dumps(
                {"total": total, "hotComments": hot_comments, "comments": all_comments},
                ensure_ascii=False,
                indent=2,
            )
        )
    elif not all_comments:
        print("暂无评论")
    return 0


def cmd_playlist(args):
    http = Http(cookie=args.cookie)
    tracks, name = get_playlist(http, args.playlist_id)
    if args.json:
        print(json.dumps({"name": name, "tracks": tracks}, ensure_ascii=False, indent=2))
        return 0
    print(f"歌单：{name}（共 {len(tracks)} 首）")
    for i, song in enumerate(tracks, 1):
        print(format_song(song, i))
    return 0


def main(argv=None):
    _safe_stdout()
    parser = argparse.ArgumentParser(
        prog="netease_music",
        description="网易云音乐爬虫：搜索 / 下载 / 歌词 / 评论 / 歌单",
    )
    parser.add_argument(
        "--cookie",
        help="登录 Cookie（MUSIC_U=...; __csrf=...），也可用环境变量 NCM_COOKIE",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="子命令")

    p = sub.add_parser("search", help="按关键词搜索歌曲")
    p.add_argument("keyword")
    p.add_argument("--limit", type=int, default=20, help="每页数量（默认 20）")
    p.add_argument("--offset", type=int, default=0, help="起始偏移（默认 0）")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("download", help="按歌曲 ID 下载 MP3")
    p.add_argument("song_id", type=int)
    p.add_argument("-o", "--outdir", default="music", help="保存目录（默认 music/）")
    p.add_argument(
        "--br",
        type=int,
        default=320000,
        choices=(96000, 128000, 192000, 320000),
        help="目标码率（默认 320000，不可用时自动降级）",
    )
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的文件")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("lyric", help="获取歌词（含翻译）")
    p.add_argument("song_id", type=int)
    p.add_argument("-o", "--out", help="保存到文件（默认打印到屏幕）")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p.set_defaults(func=cmd_lyric)

    p = sub.add_parser("comments", help="抓取歌曲评论")
    p.add_argument("song_id", type=int)
    p.add_argument("--pages", type=int, default=1, help="抓取页数（默认 1）")
    p.add_argument("--limit", type=int, default=20, help="每页条数（默认 20）")
    p.add_argument("--offset", type=int, default=0, help="起始偏移（默认 0）")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p.set_defaults(func=cmd_comments)

    p = sub.add_parser("playlist", help="按歌单 ID 导出歌曲列表")
    p.add_argument("playlist_id", type=int)
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p.set_defaults(func=cmd_playlist)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    args.cookie = args.cookie or os.environ.get("NCM_COOKIE", "")
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)
