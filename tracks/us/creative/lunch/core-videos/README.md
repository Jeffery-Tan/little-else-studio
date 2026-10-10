# US 午餐包：核心视频档案

2026-09-14，Jeffery要求将核心视频一并提交到现有私有仓库。本目录收录7条原文件副本，共84,914,880字节（约85 MB / 80.98 MiB），通过Git LFS保存。没有重新编码、剪辑、配音或生成；本机原文件保留。来源路径、SHA256、字节数和解码检查见[清单](manifest.json)。

**功能介绍采用版：[14.4秒版](reference-remake-v2-baseline-14p4s.mp4)。** 2026-09-15展示后，Jeffery明确“功能介绍的先按这版来定”。其余风格的制作顺序、暂停与失败状态只从[当前入口](../../../docs/lunch-video-current-state.md)读取；本目录保留2026-09-14收录的7条档案，不代表所有后续试验。

| 视频 | 本次收录用途 | 判断依据 |
|---|---|---|
| [原参考](reference-original.mp4) | 参考的画面与声音结构；原文件约15.10秒，开头缺音限制保留 | [原参考音频研究](../R15-commerce-v2/reference-audio-study.md) |
| [Oumomo对照](reference-oumomo.mp4) | 用户提供的带货观感比较片；不作为本款商品结构依据 | [对照定义](../review-calibration-v1/evaluation-key.json) |
| [commerce-v2比较版](commerce-v2-review-15s.mp4) | 用户曾评价“进步很大了”；原有重复动作、声画问题和收尾慢放仍存在 | [复盘与原剪辑说明](../R15-commerce-v2/result-review.md) |
| [复刻v1原片](reference-remake-v1-original.mp4) | 重复拿放、自动合链与声画错位的失败样本，也是审核校准对照 | [失败复盘](../R15-reference-remake-v1/result-review.md) |
| [复刻v2原片](reference-remake-v2-original.mp4) | 14.4秒基线的源片；保留串入参考商品等问题，瑕疵容错按用户校准判断 | [原片复盘](../R15-reference-remake-v2/result-review.md) |
| [14.4秒基线](reference-remake-v2-baseline-14p4s.mp4) | 承接明确删串图要求的可用性比较基线，保留42词原生声音 | [编辑与采用说明](../R15-reference-remake-v2/edit-review.md) |
| [节奏转译原片](reference-grammar-v1-original.mp4) | 历史失败样本：空网袋出现移动薄片、主袋自动合链；补救候选被阻断，原片不通过 | [对应试验复盘](../R15-reference-grammar-v1/result-review.md) |

参考片仅用于本私有项目内研究与比较；2026-09-15的[音轨研究](../R25-content-plan-v1/reference-audio-study.md)才将原参考同一hash绑定到T01公开视频与当时累计计数，未验证成交或因果效果，不回写为入库时已知。该关联不提供对外广告使用权或声音身份授权。其余条目的15.02秒为容器近似时长，不改变15秒生成参数。Oumomo源文件的SHA256是在本次入库时记录，未倒填成早期已经核对的hash。

## 获取与核验

Git LFS在Git中保存小型指针，实际视频由LFS存储；仓库ZIP是否带真实视频取决于GitHub的归档设置，见[GitHub官方说明](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage)。文件页可以查看或下载；克隆后若只得到指针，在仓库根目录运行：

```powershell
git lfs install --local
git lfs pull --include="tracks/us/creative/lunch/core-videos/*.mp4"
git lfs fsck
```

每个视频均完成全长解码检查；副本hash与本机源文件一致，六条还与既有记录的hash一致。检查只证明档案完整，不代表新的创意或原速听感通过。LFS指针中的SHA256与清单对应；存储及下载用量按GitHub账户实际规则计算，本次不调整付费设置。

## 收录边界

完整运行目录、密集抽帧、请求全文、完整回执和凭据继续留在Git外。11.4秒未采纳试剪、撤回的独立TTS各版及其他早期局部试验仍在本机供追溯。本目录不接Product Intel运行数据，不修改飞书任务状态，也不产生新生成、图审或发布授权。
