# US 图文＋音乐视频

2026-09-18历史技术分支，已退出当前默认流程。用户随后明确需要[带货图片＋文案](../photo-copy/README.md)，音乐在TikTok发布时添加。此处保留先前MP4编排器和技术样片供追溯，不继续安排音乐制作，也不恢复暂停的Gym。

已实现 [本地编排器](../../../../scripts/photo_music_video.py)：读取真实图片、按声明裁切与短文排版，先输出布局预览并绑定审核记录，再按精确帧数单次 H.264 编码、合入本地音乐、检查完整解码和音轨。没有图片生成、视频生成、上传或发布接口。沿用 Hypit 的素材/编排/结果分离，但本模块直接以 FFmpeg 合成，不走其已发现有损的 Normalize 链；不是声称修复了 Hypit。

原媒体留在 Git 外部旧运行目录，下列 J 盘链接仅供本机历史追溯；本轮没有复制或核验这些媒体文件。

## 看实际结果

- [9 秒技术样片审阅页](J:/E-Business-Video-Data/US/photo-music-pilot-20260918/render-v1/review.html)
- [MP4](J:/E-Business-Video-Data/US/photo-music-pilot-20260918/render-v1/photo-music.mp4) / [三页布局](J:/E-Business-Video-Data/US/photo-music-pilot-20260918/render-v1/layout.jpg)
- [实际输入配方](J:/E-Business-Video-Data/US/photo-music-pilot-20260918/manifest.json) / [输出检查](J:/E-Business-Video-Data/US/photo-music-pilot-20260918/render-v1/verification.json) / [本次观察与限制](pilot-review.md)

输入为 US / LUN / 1688 Offer 1069569802551 / 25L Gray 的既有供应商原图，不是 AI 生成的容量证明。用同款场景图、局部外观和 SKU 图验证三页布局；3 秒一页，共9秒。标题仅有“Lunch outside?”、“Take a closer look.”和“Meet the gray 25L.”。这是技术占位表达，没有独立购买理由、真实目标受众验证或营销通过结论，不把它当作本次案例学习已奏效的证据。

## 本地执行

依赖现有 Python + Pillow、FFmpeg、FFprobe；本次未安装依赖。Windows 已验证 Python 为 `J:/E-Business-Video-Data/US/workbench-pilot-v1/runtime-venv/Scripts/python.exe`。

```powershell
$py = 'J:/E-Business-Video-Data/US/workbench-pilot-v1/runtime-venv/Scripts/python.exe'
$manifest = 'J:/E-Business-Video-Data/US/photo-music-pilot-20260918/manifest.json'
$out = 'J:/E-Business-Video-Data/US/photo-music-pilot-20260918/render-v2'
& $py scripts/photo_music_video.py validate --manifest $manifest
& $py scripts/photo_music_video.py prepare --manifest $manifest --out $out
# 实际查看 layout.jpg 后，填写 layout-review.json，再执行：
& $py scripts/photo_music_video.py render --manifest $manifest --out $out --layout-approval "$out/layout-review.json"
& $py -m unittest discover -s tests -p test_photo_music_video.py -v
```

每次使用新的输出目录；保护原媒体和已导出版本，不自动覆盖。`layout-review.json` 必须含 `approved: true`、实际 `reviewer`、`reviewed_at`，以及 `layout.json` 的 `manifest_sha256` 和 `preview_sha256`，另以 `layout_sha256` 绑定 `layout.json` 文件本身，防止审核后交换顺序或改帧数。这是实际版式自检的留存，不要求新增用户逐页审批，不能伪写成 Jeffery 创意确认。修改配方、原图、字体、布局或帧图都会使旧审核不再可执行。首份技术样片保留其历史审核，不能用该旧记录在新增绑定规则下重新渲染。

## 输入与音乐

配方 schema 1 当前支持 US、720×1280、30fps、2–8页、单页45–150帧。每页包含同一 `variant`、本地 `image.path/sha256/rights`、可选原图像素 `crop`、`headline`、`frames`、`evidence` 和 `new_information`。字体也固定路径和哈希；长度过多或裁切越界会阻断，而非静默缩小文字或裁掉商品。只有确定性本地技术步骤，没有自动创意评分。

正式本地音乐输入使用：

```json
{
  "kind": "licensed_file",
  "path": "ABSOLUTE_LOCAL_MUSIC_PATH",
  "sha256": "EXACT_SHA256",
  "license": {
    "holder": "实际权利主体",
    "source": "取得来源或音乐曲目页面",
    "scope": "US_TikTok_commercial",
    "verified_by": "实际核验者",
    "verified_at": "实际核验时间",
    "evidence": {"path": "ABSOLUTE_LICENSE_EVIDENCE_PATH", "sha256": "EXACT_SHA256"}
  }
}
```

音乐必须有可解码音轨且覆盖全长，不自动拉伸、循环或下载。代码检查字段和文件哈希，**不能判断授权文书是否真实有效或是否覆盖特定账号/地区/广告场景**；该实质核验由记录的审核者负责。`commercial_local` 模式同时要求每张图片的商业授权证据和音乐作用范围。供应图持有不等于已取得广告权利；当前只有内部研究范围，不允许将样片标为商业可发布。

样片明确使用 `technical_beats` / 120 BPM：本地算法合成的正弦鼓点与高频节拍，不复制歌曲，不是 Jeffery 选定的商业配乐。该模式不能进入 `commercial_local`。测试节拍只有技术节律作用，不宣称听感和音乐品味通过。

## 如何接入案例学习

本分支共用 [案例学习](../learning/README.md) 的商品、受众、购买理由及证据限制，但具体片型单列 `photo_music`，不把达人口播规则照搬为短标题规则。下一条正式策划须说明：首图建立什么期待、每页新增什么购买信息、图片能否证明、读完需要多久、音乐与切换如何配合。当前模块不会自动发明这些答案。

后续用户认可的图文视频进案例库后，记录真实来源、逐页时间与文字、声音可观察范围，以及与结果的对照。单次导出成功只证明本地制作链可运行；营销创意、音乐选择、具体发布和市场反馈仍单独验收。
