"""Build a read-only local US creative research view from existing evidence.

No server, task state, network, paid API or automatic media learning is introduced.
"""
import argparse
import html
import hashlib
import json
import shutil
from pathlib import Path

from creative_learning import DEFAULT, creator_library, read, validate

ROOT = Path(__file__).resolve().parents[1]


RESEARCH_DOCS = (
    ('commerce-methods-research.md', '带货方法研究：购买处境、证据与人物关系'),
    ('photo-copy-methods-research.md', '图文研究：图片、图内文案与 caption'),
    ('tiktok-field-study.md', 'TikTok 实地观察与 PDP 静态研究'),
    ('workflow-skills.md', '方法如何进入实际工作流与 Skill'),
)
SKILLS = ('us-commerce-strategy', 'us-creator-video', 'us-photo-copy')


def check_photo_copy(source):
    """Verify an actual compiled pack and all bound image copies before copying."""
    source = Path(source).resolve()
    for name in ('index.html', 'pack.json', 'caption.txt', 'prompts.md'):
        if not (source / name).is_file():
            raise ValueError(f'Missing photo/copy artifact: {name}')
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('Photo/copy pack cannot contain symbolic links')
    pack = read(source / 'pack.json')
    if pack.get('market') != 'US' or pack.get('music') != 'publish-time-on-TikTok':
        raise ValueError('Photo/copy pack must be US with music added on TikTok')
    for page in pack['pages']:
        if page['status'] == 'bound':
            asset = page['image']
            relative = Path(asset['delivery_path'])
            path = (source / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(source) or not path.is_file():
                raise ValueError('Photo/copy image must be inside its delivery pack')
            if hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
                raise ValueError('Photo/copy delivered image hash changed')
        elif page['status'] != 'planned':
            raise ValueError('Unknown photo/copy image state')
    return pack


def research_panel(docs):
    learning = ROOT / 'tracks/us/creative/learning'
    sections = []
    for filename, title in RESEARCH_DOCS:
        source = learning / filename
        if source.is_file():
            shutil.copy2(source, docs / filename)
            body = source.read_text(encoding='utf-8-sig')
            sections.append(f'<article class="card"><h2>{html.escape(title)}</h2><a href="{filename}">打开实际研究正文</a><details><summary>在此阅读</summary><pre class="doc">{html.escape(body)}</pre></details></article>')
    skill_links = []
    for name in SKILLS:
        source = ROOT / '.agents/skills' / name / 'SKILL.md'
        if source.is_file():
            filename = name + '-SKILL.md'
            shutil.copy2(source, docs / filename)
            skill_links.append(f'<a href="{filename}">{html.escape(name)} · SKILL.md</a>')
    panel = '<div class="grid">' + ''.join(sections) + '</div>' if sections else '<p class="empty">研究文件尚未落盘，本页不虚构学习结果。</p>'
    panel += '<h2 style="margin-top:30px">实际 Skill 正文</h2>'
    panel += '<div class="links">' + ''.join(skill_links) + '</div>' if skill_links else '<p class="empty">尚无可链接的 Skill 文件。</p>'
    return panel


def build(out, photo, comparison, brief, photo_copy_run=None):
    library = read(DEFAULT)
    errors = validate(library)
    if errors:
        raise ValueError('; '.join(errors))
    out = out.resolve()
    if out.is_relative_to(ROOT):
        raise ValueError('Generated research pages/media must remain outside Git')
    if photo_copy_run is not None and out.is_relative_to(Path(photo_copy_run).resolve()):
        raise ValueError('Hub output cannot be inside the source photo/copy pack')
    inputs = [photo / name for name in ('review.html', 'photo-music.mp4', 'layout.jpg', 'verification.json')]
    inputs += [comparison / name for name in ('comparison.json', 'comparison.md')]
    inputs += [brief / name for name in ('review.md', 'prompt.txt', 'manifest.json')]
    for path in inputs:
        if not path.is_file():
            raise ValueError(f'Missing actual artifact: {path}')
    photo_copy = check_photo_copy(photo_copy_run) if photo_copy_run is not None else None
    out.mkdir(parents=True, exist_ok=False)
    docs = out / 'documents'
    docs.mkdir()
    if photo_copy_run is not None:
        shutil.copytree(photo_copy_run, out / 'photo-copy')
    for folder, source, filenames in (
        ('photo-music', photo, ('review.html', 'photo-music.mp4', 'layout.jpg', 'verification.json')),
        ('comparison', comparison, ('comparison.json', 'comparison.md')),
        ('brief', brief, ('review.md', 'prompt.txt', 'manifest.json'))):
        destination = out / folder
        destination.mkdir()
        for filename in filenames:
            shutil.copy2(source / filename, destination / filename)
    for name in ('reference-original.mp4', 'reference-oumomo.mp4', 'reference-remake-v1-original.mp4'):
        shutil.copy2(ROOT / 'tracks/us/creative/lunch/core-videos' / name, out / 'comparison' / name)
    for filename in ('README.md', 'batch-001.md', 'strategy-guide.md', 'execution.md'):
        shutil.copy2(ROOT / 'tracks/us/creative/learning' / filename, docs / filename)
    guide = (ROOT / 'tracks/us/creative/learning/strategy-guide.md').read_text(encoding='utf-8-sig')
    # Keep unseen evaluation cases out of the ordinary learning view.
    visible = creator_library(library)
    # JSON is data; escape script-closing sequences. Rendering uses textContent/escaping.
    data = json.dumps(visible, ensure_ascii=False).replace('<', '\\u003c')
    page = TEMPLATE.replace('__LIBRARY__', data).replace('__GUIDE__', html.escape(guide))
    page = page.replace('__CASE_COUNT__', str(sum(c['partition'] == 'calibration' for c in visible['cases'])))
    page = page.replace('__METHOD_COUNT__', str(len(visible['methods'])))
    page = page.replace('__RESEARCH_PANEL__', research_panel(docs))
    research_count = 0
    for name in ('commerce-methods-sources.json', 'photo-copy-methods-sources.json'):
        source = ROOT / 'tracks/us/creative/learning' / name
        if source.is_file():
            records = read(source).get('sources', [])
            research_count += len(records)
            shutil.copy2(source, docs / name)
    page = page.replace('__RESEARCH_COUNT__', str(research_count))
    field_study = ROOT / 'tracks/us/creative/learning/tiktok-field-study.md'
    page = page.replace('__FIELD_STUDY_NOTE__', '另有2个PDP静态研究，属于商品页观察，不是完整观看带货视频。' if field_study.is_file() else 'PDP静态研究尚未接入本入口，不据此增加视频观看计数。')
    if photo_copy is not None:
        bound = sum(p['status'] == 'bound' for p in photo_copy['pages'])
        photo_panel = f'<span class="status">策划包已编译</span><h2 style="margin-top:20px">{len(photo_copy["pages"])}页图片与文案 · {bound}张图片已绑定</h2><p>已绑定不等于已审核；未生成的页面会明确显示 planned。没有自动生成图片或判定创意通过。</p><div class="links"><a href="../photo-copy/index.html">打开有序图片与文案审阅</a><a href="../photo-copy/caption.txt">发布 caption</a><a href="../photo-copy/prompts.md">逐页文案与生图提示词</a><a href="../photo-copy/pack.json">完整策划与证据</a></div>'
        photo_state = '已编译图片与文案'
    else:
        photo_panel = '<span class="status pending">尚未编译图文策划包</span><p>本入口没有接入实际图片＋文案交付，不展示虚构的成图或预览。方法研究可先阅读；具体稿件编译后再接入。</p>'
        photo_state = '等待接入实际策划包'
    page = page.replace('__PHOTO_COPY_PANEL__', photo_panel).replace('__PHOTO_COPY_STATE__', photo_state)
    (docs / 'index.html').write_text(page, encoding='utf-8')
    return docs / 'index.html'


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Little Else · 创意研究室</title><style>
:root{color-scheme:dark;--bg:#101113;--panel:#191b1e;--line:#303337;--muted:#afb1b6;--text:#f2eee7;--accent:#eacb8b;--green:#a3c2a7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.7 "Segoe UI","Microsoft YaHei",sans-serif}button,input,select{font:inherit}button,a,input,select{outline-offset:5px}a{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:4px}.shell{max-width:1560px;margin:auto;display:grid;grid-template-columns:230px 1fr;min-height:100vh}aside{border-right:1px solid var(--line);padding:34px 22px;position:sticky;top:0;height:100vh}.brand{font-size:18px;letter-spacing:.13em}.sub{color:var(--muted);font-size:12px;letter-spacing:.1em;margin:5px 0 35px}nav{display:grid;gap:9px}nav button{border:0;background:transparent;color:var(--muted);text-align:left;padding:12px 15px;border-radius:6px;cursor:pointer}nav button[aria-selected=true]{background:#2e2b24;color:var(--accent)}nav button:hover{background:#252729;color:var(--text)}.aside-note{font-size:12px;color:var(--muted);position:absolute;bottom:30px;left:36px;right:30px}main{padding:35px 48px 70px;min-width:0}.topline{display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--line);padding-bottom:20px;letter-spacing:.05em}.tag{font-size:12px;color:var(--accent)}h1{font-weight:500;font-size:38px;letter-spacing:-.03em;line-height:1.3;margin:32px 0 14px}h2{font-size:23px;font-weight:500;margin:0 0 13px}h3{font-size:17px;font-weight:500;margin:0 0 10px}p{margin:10px 0}.lead{max-width:780px;color:var(--muted);font-size:16px}.metrics{display:flex;gap:45px;margin:35px 0}.metric strong{font-size:32px;font-weight:400;display:block}.metric span{color:var(--muted);font-size:12px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:25px}.card{background:var(--panel);border:1px solid var(--line);padding:26px;border-radius:8px}.card p{color:var(--muted)}.eyebrow{font-size:11px;letter-spacing:.14em;color:var(--accent);margin-bottom:12px}.status{display:inline-block;border:1px solid #455a48;color:var(--green);font-size:11px;padding:2px 8px;border-radius:20px}.pending{border-color:#695c3e;color:var(--accent)}.card-link{display:inline-block;margin-top:14px;background:none;border:0;color:var(--accent);padding:0;cursor:pointer;text-align:left}.note{border-left:2px solid var(--accent);padding:3px 0 3px 18px;color:var(--muted);margin:30px 0}section[hidden]{display:none}.tools{display:flex;gap:12px;margin:25px 0}.tools input,.tools select{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:5px;padding:11px 14px}.tools input{width:100%;max-width:490px}.case-list{display:grid;gap:12px}.case{background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:20px 24px}.case summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;gap:12px;align-items:center}.case summary:after{content:'＋';color:var(--accent)}.case[open] summary:after{content:'−'}.case small{color:var(--muted);display:block;margin-top:5px}.case .body{border-top:1px solid var(--line);margin-top:17px;padding-top:14px}.case ul{padding-left:20px}.case li{margin-bottom:9px;color:var(--muted)}.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}.chip{font:11px/1.6 monospace;background:#292c30;padding:3px 7px;color:#c8cad0;border-radius:3px}.empty{color:var(--muted);padding:30px 0}.steps{counter-reset:step;display:grid;gap:0;margin-top:24px}.step{padding:22px 0 22px 55px;border-bottom:1px solid var(--line);position:relative}.step:before{counter-increment:step;content:'0'counter(step);position:absolute;left:0;color:var(--accent);font-size:23px}.step p{color:var(--muted);margin:5px 0}.doc{white-space:pre-wrap;font:14px/1.9 "Segoe UI","Microsoft YaHei",sans-serif;overflow-wrap:anywhere;max-height:65vh;overflow:auto;background:var(--panel);border:1px solid var(--line);padding:24px;border-radius:8px}.video-layout{display:grid;grid-template-columns:minmax(230px,330px) 1fr;gap:35px;margin-top:30px}.video-layout video{width:100%;max-height:65vh;background:#090a0b;border:1px solid var(--line);border-radius:7px}.compare{display:grid;grid-template-columns:repeat(3,1fr);gap:17px;margin-top:26px}.compare video{width:100%;height:360px;background:#08090a;border:1px solid var(--line);border-radius:5px}.compare p{color:var(--muted);font-size:12px}.method{padding:18px 0;border-bottom:1px solid var(--line)}.method p{color:var(--muted)}.small{font-size:12px;color:var(--muted)}code{color:#d6d5cc;font-size:13px}.links{display:flex;flex-wrap:wrap;gap:20px;margin:22px 0}footer{margin-top:45px;border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:12px}
@media(max-width:900px){.shell{grid-template-columns:1fr}aside{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);padding:20px}.sub{margin-bottom:15px}nav{display:flex;overflow-x:auto;gap:5px}nav button{white-space:nowrap}.aside-note{display:none}main{padding:23px}.grid{grid-template-columns:1fr}.compare video{height:270px}.video-layout{grid-template-columns:1fr}.video-layout video{max-width:330px}h1{font-size:30px}}@media(max-width:560px){.compare{grid-template-columns:1fr}.compare video{height:400px}.metrics{gap:25px}.tools{flex-wrap:wrap}.case summary{align-items:flex-start}.topline{font-size:10px}}
</style></head><body><div class="shell"><aside><div class="brand">LITTLE ELSE</div><div class="sub">CREATIVE RESEARCH / US</div><nav role="tablist" aria-label="研究板块">
<button role="tab" aria-selected="true" data-tab="overview">概览</button><button role="tab" aria-selected="false" data-tab="cases">案例与方法</button><button role="tab" aria-selected="false" data-tab="research">研究方法与 Skills</button><button role="tab" aria-selected="false" data-tab="workflow">策略与制作输入</button><button role="tab" aria-selected="false" data-tab="photo">带货图片＋文案</button><button role="tab" aria-selected="false" data-tab="comparison">Oumomo 对照</button>
</nav><div class="aside-note">本地研究视图<br>素材与任务按原事实源维护<br>2026.09.18</div></aside><main>
<div class="topline"><span>从参考证据，到实际表达</span><span>US · PRIVATE WORKSPACE</span></div>
<section id="overview"><h1>把经验接进下一次创作。</h1><p class="lead">整理过去的正反案例，解释每个创作选择，并检查它有没有进入真正的制作指令。新参考和实际结果继续回到同一套证据里。</p>
<div class="metrics"><div class="metric"><strong>__CASE_COUNT__</strong><span>历史校准案例</span></div><div class="metric"><strong>__METHOD_COUNT__</strong><span>条件式方法假设</span></div><div class="metric"><strong>__RESEARCH_COUNT__</strong><span>研究来源记录（未按来源去重）</span></div></div>
<div class="grid"><article class="card"><div class="eyebrow">01 / LEARNING</div><h2>案例与方法</h2><span class="status">已可检索</span><p>保留来源、观察范围、用户究竟认可什么，以及不能照搬的部分。支持后续链接和复刻任务持续补充。</p><button class="card-link" data-tab="cases">浏览案例 →</button></article>
<article class="card"><div class="eyebrow">02 / STRATEGY</div><h2>从人物到实际指令</h2><span class="status">本地编译已验证</span><p>受众处境、购买理由、人物关系、具体台词与镜头在同一份方案里；说话方式随镜头进入Prompt。</p><button class="card-link" data-tab="workflow">查看工作流 →</button></article>
<article class="card"><div class="eyebrow">03 / PHOTO & COPY</div><h2>带货图片与文案</h2><span class="status">__PHOTO_COPY_STATE__</span><p>基于购买问题组织图片、图内文字、发布 caption 和生图提示词；音乐直接在 TikTok 发布端添加。</p><button class="card-link" data-tab="photo">查看图文工作区 →</button></article>
<article class="card"><div class="eyebrow">04 / COMPARISON</div><h2>研究可观察的差异</h2><span class="status pending">同输入材料待补</span><p>已比较旧参考、Oumomo成片和本方失败。完整对照需要对应那次请求的图片、用户稿及后台指令。</p><button class="card-link" data-tab="comparison">查看已有比较 →</button></article></div>
<p class="small">研究来源记录允许同一来源因不同研究问题重复登记，不等于相同数量的独立来源。__FIELD_STUDY_NOTE__</p><div class="links"><button class="card-link" data-tab="research">阅读实际方法研究与 Skills →</button></div><div class="note">还没有完成可靠原速听评、未见题或成交验证。声轨匿名对照已准备，等待该次付费批准；Gym制作保持暂停。</div></section>
<section id="cases" hidden><h1>学机制，也保留反例。</h1><p class="lead">这些是已知历史校准材料。标签用于相关性检索，不是创意评分；抽帧和ASR不会自动升级成声音、表演或转化证据。</p><div class="tools"><input id="search" aria-label="搜索案例" placeholder="搜索人物、节奏、理由、packing…"><select id="purpose" aria-label="证据用途"><option value="all">全部案例</option><option value="message">可参考：语义 / 购买理由</option><option value="storyboard">可参考：分镜</option><option value="performance">完整声画依据</option></select></div><p id="count" class="small" aria-live="polite"></p><div id="case-list" class="case-list"></div><h2 style="margin-top:35px">可被新证据修正的方法</h2><div id="methods"></div></section>
<section id="workflow" hidden><h1>先决定怎么说，再让模型执行。</h1><p class="lead">同一份方案同时约束人物关系、台词作用和镜头证据。机械一致性检查不能替代内容判断，但能发现最终输入偏离方案。</p><div class="steps"><div class="step"><h3>取回本题相关的正反例</h3><p>按处境和用途检索，只使用证据覆盖足够的案例。未知样本保持待学习。</p></div><div class="step"><h3>写清一个购买判断如何改变</h3><p>处境 → 原有办法 → 顾虑 → 本款证据 → 人物关系。具体理由比功能标签更有用。</p></div><div class="step"><h3>把表达落实到实际声画</h3><p>每段写出观众看到、听到、新理解什么。对镜、旁白和无口播按段选择，压等待、保留必要理由。</p></div><div class="step"><h3>编译、核对，再按原授权制作</h3><p>生成Prompt和版本记录；最终文字变化需重编译复查。图审、预算及完整请求仍用原执行器。当前没有自动内容放行。</p></div><div class="step"><h3>用结果修正方法</h3><p>用户核心重写、输入偏离和真实成片分别回查。一次接受、导出成功和成交有效分开记录。</p></div></div>
<div class="links"><a href="execution.md">执行说明与声轨校准</a><a href="../brief/review.md">查看编译演练</a><a href="../brief/prompt.txt">查看实际Prompt</a></div><details><summary>展开具体策略指南与新研究线索</summary><pre class="doc">__GUIDE__</pre></details><p class="small">编译演练不是可执行制作候选；完整听评和创意质量没有因此通过。新发现的官方案例只作研究线索，尚未计入已学习样本。</p></section>
<section id="photo" hidden><h1>带货图片＋文案。</h1><p class="lead">先回答观众的购买问题，再组织逐页图片、图内文字与发布 caption。这里不制作配乐或视频，音乐直接在 TikTok 发布时选择。</p><div class="card">__PHOTO_COPY_PANEL__</div><div class="note">图片状态、文案策划和实际审核分别记录。planned是尚未生成，bound是已绑定现有图片，两者都不表示用户已审核或商业表达已通过。工具不替代对成图、文字可读性和购买作用的实际检查。</div><div class="links"><button class="card-link" data-tab="research">查看图片与文案的方法依据 →</button><a href="../photo-music/review.html">历史记录：9秒图文音乐技术试验（不作为当前方向）</a></div></section>
<section id="research" hidden><h1>有依据的方法，也保留边界。</h1><p class="lead">将原始研究、可用性观察与平台说明转成具体创作决策。来源记录不是独立案例数，PDP静态观察不是视频观看；理论与结构化Skill也不自动证明营销有效。</p>__RESEARCH_PANEL__</section>
<section id="comparison" hidden><h1>看得见的，才纳入比较。</h1><p class="lead">旧材料已完成媒体、来源与既有语义核对。下列三条并未证明使用相同输入，不能据此反推隐藏模型或架构。</p><div class="compare"><article><h3>T01 原参考</h3><video controls playsinline preload="metadata" src="../comparison/reference-original.mp4"></video><p>约15.10秒 · 开头约5.72秒缺音</p></article><article><h3>Oumomo 旧成片</h3><video controls playsinline preload="metadata" src="../comparison/reference-oumomo.mp4"></video><p>约15.02秒 · 当次输入与后台Prompt缺失</p></article><article><h3>本方 R15 v1</h3><video controls playsinline preload="metadata" src="../comparison/reference-remake-v1-original.mp4"></video><p>约15.02秒 · 已知失败对照</p></article></div><div class="note">已有对照表明：词数预算足够、加入视频参考，也不能替代句群与动作的紧凑编排。具体语气和表演仍需可靠听评。</div><div class="links"><a href="../comparison/comparison.md">受限比较报告</a><a href="../comparison/comparison.json">材料与缺口明细</a></div><p>之后提供同一请求的参考视频、图片、原始提示词、后台最终提示词和成片，助手会接入此对照。可见参数保留，隐藏部分保持未知。</p></section>
<footer>本地可执行工具与证据视图 · 无后台采集、无自动付费、未发布<br>新增素材由助手在正常任务中整理，用户无需填写表格。</footer>
</main></div><script type="application/json" id="library">__LIBRARY__</script><script>
const library=JSON.parse(document.getElementById('library').textContent);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function feedbackHistory(c){
  const history=c.feedback_history||[];
  if(!history.length)return '<p class="small">尚无追加反馈；没有新反馈不表示采用。</p>';
  return '<h3>追加反馈历史</h3><p class="small">按具体版本和范围理解；用户意图不自动产生执行授权，旧反馈不被覆盖。</p>'+history.map(e=>`<article class="method"><p><strong>原话 / 忠实摘要：</strong>${esc(e.quote)}</p><p class="small">产物版本：${esc(e.artifact_version)} · 日期：${esc(e.recorded_at)} · 学习库版本：${esc(e.library_version)}<br>原 run：${esc(e.run)} · 来源事件：${esc(e.source_event)}<br>来源：${esc(e.source)}</p><p>范围：${esc(e.scope)}<br>用户实际要求：${esc(e.action)}</p><p>已观察事实：${esc(e.observation)}<br>原因假设（非事实）：${esc(e.hypothesis??'null（尚未确定）')}<br>改进效果：${esc(e.improvement_effect??'null（未观察到改善）')}</p></article>`).join('');
}
function show(id){if(!['overview','cases','research','workflow','photo','comparison'].includes(id))id='overview';document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!==id);document.querySelectorAll('nav button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===id)));document.querySelectorAll('video').forEach(v=>v.pause());history.replaceState(null,'','#'+id);window.scrollTo({top:0,behavior:'instant'});}
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>show(b.dataset.tab)));
function draw(){const q=document.getElementById('search').value.toLowerCase().trim(),p=document.getElementById('purpose').value;const cases=library.cases.filter(c=>(p==='all'||(c.eligible_for||[]).includes(p))&&JSON.stringify(c).toLowerCase().includes(q));document.getElementById('count').textContent=cases.length+' 条符合条件';document.getElementById('case-list').innerHTML=cases.length?cases.map(c=>`<details class="case"><summary><div><strong>${esc(c.title)}</strong><small>${esc(c.id)} · ${esc(c.partition)} · ${esc(c.platform)}</small></div></summary><div class="chips">${c.tags.map(t=>`<span class="chip">${esc(t)}</span>`).join('')}</div><div class="body"><h3>传播任务（分析假设）</h3>${Object.entries(c.strategy||{}).map(([k,v])=>`<p><span class="small">${esc(k)}</span><br>${esc(v)}</p>`).join('')}<h3>实际证据</h3><ul>${c.evidence.map(e=>`<li><span class="tag">${esc(e.kind)}</span> ${esc(e.observation)}</li>`).join('')}</ul><h3>不可迁移与未知</h3><ul>${c.limits.map(x=>`<li>${esc(x)}</li>`).join('')}</ul><p class="small">历史认可摘要（原字段）：${esc(c.user_feedback?.scope||'无具体认可记录')}<br>成交效果：${c.market_effect?'见原证据':'未知'}</p>${feedbackHistory(c)}</div></details>`).join(''):'<div class="empty">暂无满足证据覆盖的案例。不会用ASR或抽帧替代完整声画听评。</div>';}
document.getElementById('search').addEventListener('input',draw);document.getElementById('purpose').addEventListener('change',draw);document.getElementById('methods').innerHTML=library.methods.map(m=>`<article class="method"><span class="tag">${esc(m.id)} · ${esc(m.status)}</span><h3>${esc(m.decision)}</h3><p>适用：${esc(m.when)}</p><p>限制 / 反例：${esc(m.countercondition)}</p></article>`).join('');draw();show(location.hash.slice(1));
</script></body></html>'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--photo-run', type=Path, required=True)
    parser.add_argument('--comparison-run', type=Path, required=True)
    parser.add_argument('--brief-run', type=Path, required=True)
    parser.add_argument('--photo-copy-run', type=Path, help='Actual compiled photo/copy pack; omitted means not yet compiled')
    args = parser.parse_args()
    print(build(args.output_dir, args.photo_run, args.comparison_run, args.brief_run, args.photo_copy_run))
