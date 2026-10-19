# -*- coding: utf-8 -*-
"""
补翻（translation repair）：
翻译完成后扫描 merge_translate_zh.tex 中残留的未翻译英文——
  1) LLM 漏翻回退的正文段落（管线标记为 TRANSFORM 但回复里被丢掉，静默回退英文）
  2) 被保护区规则挡住的 \section/\subsection/\subsubsection 标题
  3) 被“42行以内 begin-end 一律保护”兜底规则误伤的 ack 致谢环境
仅对这些目标重新调用 LLM 翻译、回填、重新编译，并把新 PDF 重新归档到译文库。
任何一步失败都回退到原始 tex/pdf，不影响已有产物。
"""
import os
import re
import shutil
from loguru import logger

pj = os.path.join

# 这些环境内部不翻译（图片、表格、公式、代码、算法、参考文献等）
PROTECTED_ENVS = {
    'figure', 'figure*', 'wrapfigure', 'wrapfigure*', 'table', 'table*', 'wraptable',
    'tabular', 'tabularx', 'align', 'align*', 'equation', 'equation*',
    'multline', 'multline*', 'gather', 'gather*', 'tikzpicture', 'groupplot', 'axis',
    'algorithm', 'algorithmic', 'lstlisting', 'verbatim', 'thebibliography',
    'minipage', 'minipage*', 'comment',
}  # 注意: 不要包含 document（正文全在 document 内）

TRANSLATE_INPUT = (
    "Below is a section from an English academic paper, translate it into Chinese. "
    "Do not modify any latex command such as \\section, \\cite, \\begin, \\item and equations. "
    "Answer me only with the translated text:"
)
TRANSLATE_SYS = "You are a professional translator."
MAX_TARGETS = 120  # 单次补翻的目标上限，防止异常情况下失控


def _strip_for_check(text):
    t = re.sub(r'\\[a-zA-Z@]+\*?(\[[^\]]*\])?', ' ', text)
    t = re.sub(r'[{}$\\&~^_]', ' ', t)
    return t


def _is_untranslated_prose(text):
    """英文占优且完全无中文的正文段落"""
    t = _strip_for_check(text)
    words = re.findall(r'[A-Za-z]{3,}', t)
    if len(words) < 6:
        return False
    return not re.search(r'[一-鿿]', t)


def _find_targets(tex):
    """返回修复目标 [(start, end, original, kind)]，kind ∈ {'title', 'paragraph'}"""
    targets = []

    # <------ 1) 未翻译的章节标题 ------>
    for m in re.finditer(r'\\(section|subsection|subsubsection)\*?\{([^{}]+)\}', tex):
        content = m.group(2)
        if re.search(r'[一-鿿]', content):
            continue
        if len(re.findall(r'[A-Za-z]{2,}', content)) < 1:
            continue
        # 只替换花括号内的文本
        targets.append((m.start(2), m.end(2), content, 'title'))

    # <------ 2) 未翻译的正文段落（含 ack 环境内部） ------>
    lines = tex.split('\n')
    maketitle_line = None
    for i, line in enumerate(lines):
        if '\\maketitle' in line:
            maketitle_line = i
            break
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    env_stack = []
    paragraphs = []  # (start_offset, end_offset, text, protected)
    cur_start, cur_lines, cur_protected = None, [], False
    for i, line in enumerate(lines):
        stack_before = list(env_stack)
        line_protected = any(e in PROTECTED_ENVS for e in stack_before)
        # 更新环境栈（按出现顺序处理本行的 begin/end）
        for m in re.finditer(r'\\(begin|end)\{([^}]+)\}', line):
            if m.group(1) == 'begin':
                env_stack.append(m.group(2))
            elif m.group(2) in env_stack:
                # 弹出到匹配环境为止（容错未闭合/错序），找不到则忽略
                while env_stack and env_stack[-1] != m.group(2):
                    env_stack.pop()
                if env_stack:
                    env_stack.pop()
        if maketitle_line is not None and i <= maketitle_line:
            # \maketitle 及之前的标题/作者区保持英文（学术惯例）
            line_protected = True
        if line.strip() == '':
            if cur_lines:
                paragraphs.append((cur_start, offsets[i], '\n'.join(cur_lines), cur_protected))
                cur_lines, cur_protected = [], False
                cur_start = None
            continue
        if cur_start is None:
            cur_start = offsets[i]
        cur_lines.append(line)
        cur_protected = cur_protected or line_protected
    if cur_lines:
        paragraphs.append((cur_start, len(tex), '\n'.join(cur_lines), cur_protected))

    for start, end, text, protected in paragraphs:
        if protected:
            continue
        if re.search(r'\\(includegraphics|bibitem|bibliography)', text):
            continue
        if _is_untranslated_prose(text):
            targets.append((start, end, text, 'paragraph'))

    targets.sort(key=lambda t: t[0])
    return targets[:MAX_TARGETS]


def _latex_sanity_ok(original, translated):
    """翻译结果的基本完整性校验：环境配对、引用数量、花括号平衡"""
    if re.search(r'[一-鿿]', translated) is None:
        return False
    if translated.strip() == original.strip():
        return False
    if len(translated) < len(original) * 0.15 or len(translated) > len(original) * 4:
        return False
    for cmd in ('begin', 'end'):
        o = sorted(re.findall(rf'\\{cmd}\{{([^}}]+)\}}', original))
        t = sorted(re.findall(rf'\\{cmd}\{{([^}}]+)\}}', translated))
        if o != t:
            return False
    if original.count('\\cite') != translated.count('\\cite'):
        return False
    if translated.count('{') != translated.count('}'):
        return False
    return True


def _translate_one(text, llm_kwargs, kind):
    from request_llms.bridge_all import predict_no_ui_long_connection
    prompt = TRANSLATE_INPUT
    if kind == 'title':
        prompt = prompt.replace('a section from an English academic paper', 'a paper section title')
    result = predict_no_ui_long_connection(
        inputs=f"{prompt}\n\n{text}",
        llm_kwargs=llm_kwargs, history=[], sys_prompt=TRANSLATE_SYS,
        console_silence=True,
    )
    result = result.strip()
    # 去掉模型可能包裹的 markdown 代码块
    m = re.match(r'^```(?:latex|tex)?\s*\n?(.*?)\n?```$', result, re.DOTALL)
    if m:
        result = m.group(1).strip()
    return result


def repair_translation(project_folder, llm_kwargs, chatbot=None):
    """
    对 project_folder（workfolder）中的 merge_translate_zh.tex 执行补翻。
    成功：重新编译、更新 translation/ 缓存、重新归档译文库，返回统计 dict。
    失败：回退到原始文件，返回 None。
    """
    from crazy_functions.latex_fns.latex_toolbox import compile_latex_with_timeout, merge_pdfs

    tex_path = pj(project_folder, 'merge_translate_zh.tex')
    bak_path = pj(project_folder, 'merge_translate_zh.tex.repair_bak')
    if not os.path.exists(tex_path):
        logger.info('补翻: 未找到 merge_translate_zh.tex，跳过')
        return None

    with open(tex_path, 'r', encoding='utf-8') as f:
        tex = f.read()
    targets = _find_targets(tex)
    n_title = sum(1 for t in targets if t[3] == 'title')
    n_para = len(targets) - n_title
    if len(targets) == 0:
        logger.info('补翻: 未发现漏翻目标')
        return {'translated': 0, 'failed': 0}
    logger.info(f'补翻: 发现 {n_para} 个漏翻段落, {n_title} 个漏翻标题')

    shutil.copyfile(tex_path, bak_path)
    translated_count, failed_count = 0, 0
    replacements = []
    for start, end, original, kind in targets:
        try:
            result = _translate_one(original, llm_kwargs, kind)
            if not _latex_sanity_ok(original, result):
                # 重试一次
                result = _translate_one(original, llm_kwargs, kind)
            if _latex_sanity_ok(original, result):
                replacements.append((start, end, result))
                translated_count += 1
            else:
                failed_count += 1
                logger.info(f'补翻: 目标翻译未通过校验，保留原文: {original[:60]}...')
        except Exception as e:
            failed_count += 1
            logger.info(f'补翻: 调用失败，保留原文: {e}')

    if translated_count == 0:
        logger.info('补翻: 没有成功翻译任何目标，不改动文件')
        try:
            os.remove(bak_path)
        except OSError:
            pass
        return {'translated': 0, 'failed': failed_count}

    # 从后往前应用替换，保证偏移不失效
    new_tex = tex
    for start, end, result in sorted(replacements, key=lambda r: -r[0]):
        new_tex = new_tex[:start] + result + new_tex[end:]
    with open(tex_path, 'w', encoding='utf-8') as f:
        f.write(new_tex)

    # <------ 重新编译 ------>
    compile_cmd = 'pdflatex -interaction=batchmode -file-line-error merge_translate_zh.tex'
    ok = compile_latex_with_timeout(compile_cmd, project_folder)
    ok = compile_latex_with_timeout(compile_cmd, project_folder)
    pdf_path = pj(project_folder, 'merge_translate_zh.pdf')
    if not (ok and os.path.exists(pdf_path)):
        logger.error('补翻: 重新编译失败，回退到原始 tex')
        shutil.copyfile(bak_path, tex_path)
        compile_latex_with_timeout(compile_cmd, project_folder)
        compile_latex_with_timeout(compile_cmd, project_folder)
        return None

    # <------ 更新缓存与译文库 ------>
    translation_dir = pj(project_folder, '..', 'translation')
    origin_pdf = pj(project_folder, 'merge.pdf')
    if os.path.exists(translation_dir):
        shutil.copyfile(pdf_path, pj(translation_dir, 'translate_zh.pdf'))
    if os.path.exists(origin_pdf):
        try:
            concat_pdf = pj(project_folder, 'comparison.pdf')
            merge_pdfs(origin_pdf, pdf_path, concat_pdf)
            if os.path.exists(translation_dir):
                shutil.copyfile(concat_pdf, pj(translation_dir, 'comparison.pdf'))
        except Exception as e:
            logger.info(f'补翻: 对照PDF生成失败（不影响译文）: {e}')
    try:
        from toolbox import archive_translation_to_library
        if os.path.exists(translation_dir):
            archive_translation_to_library(pj(translation_dir, 'translate_zh.pdf'))
            if os.path.exists(pj(translation_dir, 'comparison.pdf')):
                archive_translation_to_library(pj(translation_dir, 'comparison.pdf'))
    except Exception as e:
        logger.info(f'补翻: 归档失败（不影响译文）: {e}')

    stats = {'translated': translated_count, 'failed': failed_count}
    logger.info(f'补翻完成: 重翻 {translated_count} 处, 失败保留原文 {failed_count} 处')
    if chatbot is not None:
        chatbot.append(['补翻完成', f'检测到翻译遗漏并已重新翻译 {translated_count} 处（含标题与致谢），未能翻译保留原文 {failed_count} 处。新PDF已重新编译并归档到译文库。'])
    return stats
