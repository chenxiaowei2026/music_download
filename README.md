# music_download
download 网易云音乐
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
