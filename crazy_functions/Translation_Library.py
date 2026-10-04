import os
import time
import glob
import shutil
from toolbox import CatchException, update_ui
from toolbox import promote_file_to_downloadzone, disable_auto_promotion
from toolbox import get_user, get_translation_library_dir


def _fmt_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if num < 1024.0 or unit == 'GB':
            return f"{int(num)}B" if unit == 'B' else f"{num:.1f}{unit}"
        num /= 1024.0


def _list_library_files(lib_dir):
    # 返回 (子文件夹, 文件路径) 列表：含 中文译文/ 与 原文对照/ 子文件夹及根目录散落文件
    files = []
    for entry in os.listdir(lib_dir):
        if entry.startswith('.'):  # 跳过 .DS_Store 等隐藏文件
            continue
        entry_path = os.path.join(lib_dir, entry)
        if os.path.isdir(entry_path):
            files += [(entry, f) for f in glob.glob(os.path.join(entry_path, '*'))
                      if os.path.isfile(f) and not os.path.basename(f).startswith('.')]
        elif os.path.isfile(entry_path):
            files += [('', entry_path)]
    return sorted(files, key=lambda t: (t[0], os.path.basename(t[1]).lower()))


def _target_subdir(name):
    # 手动收入的文件按名称归类：对照类进 原文对照/，其余进 中文译文/
    if ('对照' in name) or ('comparison' in name.lower()):
        return '原文对照'
    return '中文译文'


@CatchException
def 查看译文库(txt, llm_kwargs, plugin_kwargs, chatbot, history, system_prompt, user_request):
    # 固定译文库：gpt_log/<用户>/译文库，翻译完成后自动归档于此
    disable_auto_promotion(chatbot)
    user_name = get_user(chatbot)
    lib_dir = get_translation_library_dir(user_name)
    chatbot.append(["函数插件功能？",
                    f"查看固定译文库（中文译文/ 与 原文对照/ 两个子文件夹分开存放）。留空输入：列出库中全部文件并在“文件下载区”显示；"
                    f"输入路径：把该文件（或文件夹内的文档）收入译文库。库的位置：`{lib_dir}`（项目根目录“译文库”快捷方式可直接打开）。"])
    yield from update_ui(chatbot=chatbot, history=history)  # 刷新界面

    # <-------------- 输入为空：列出全部译文 -------------->
    if txt.strip() == '':
        files = _list_library_files(lib_dir)
        if len(files) == 0:
            chatbot.append(["译文库为空",
                            "当前译文库中还没有文件。翻译论文后会自动存入这里；也可以输入文件路径手动收入。"])
            yield from update_ui(chatbot=chatbot, history=history)
            return
        history = []
        rows = ["| 分类 | 文件 | 大小 | 修改时间 |", "|---|---|---|---|"]
        for sub, f in files:
            size = _fmt_size(os.path.getsize(f))
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(f)))
            rows.append(f"| {sub or '未分类'} | {os.path.basename(f)} | {size} | {mtime} |")
        chatbot.append([f"译文库共 {len(files)} 个文件（中文译文/ 与 原文对照/ 分开放置）", '\n'.join(rows)])
        yield from update_ui(chatbot=chatbot, history=history)
        # 在“文件下载区”与对话中显示全部译文，点击即可查看/下载
        for sub, f in files:
            promote_file_to_downloadzone(f, chatbot=chatbot)
        chatbot.append([None, "以上译文已全部显示在右侧“文件下载区”（可能处于折叠状态），点击即可查看或下载。"])
        yield from update_ui(chatbot=chatbot, history=history)
        return

    # <-------------- 输入为路径：把文件收入译文库 -------------->
    # 校验路径安全性（仅允许 gpt_log/private_upload 内的文件，上传后的文件即在此处）
    from shared_utils.fastapi_server import validate_path_safety
    validate_path_safety(txt, chatbot.get_user())

    candidates = []
    if os.path.isdir(txt):
        for ext in ('pdf', 'md', 'html', 'docx', 'tex'):
            candidates += glob.glob(os.path.join(txt, f'**/*.{ext}'), recursive=True)
    elif os.path.exists(txt):
        candidates = [txt]
    if len(candidates) == 0:
        chatbot.append([f"解析输入: {txt}", "未找到可收入译文库的文件（支持 .pdf/.md/.html/.docx/.tex）。请先上传文件，再输入其路径。"])
        yield from update_ui(chatbot=chatbot, history=history)
        return

    added, skipped = [], []
    for f in candidates:
        sub = _target_subdir(os.path.basename(f))
        dst_dir = os.path.join(lib_dir, sub)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, os.path.basename(f))
        if os.path.abspath(dst) == os.path.abspath(f):
            skipped.append(f)  # 已在库中
            continue
        shutil.copyfile(f, dst)
        added.append(dst)
    for f in added:
        promote_file_to_downloadzone(f, chatbot=chatbot)
    msg = f"已收入 {len(added)} 个文件到译文库：\n\n" + '\n'.join(f"- {os.path.basename(os.path.dirname(f))}/{os.path.basename(f)}" for f in added)
    if skipped:
        msg += f"\n\n另有 {len(skipped)} 个文件已在库中，跳过。"
    msg += "\n\n留空输入再次运行本插件，即可查看全部译文。"
    chatbot.append(["收入完成" if added else "没有新文件", msg])
    yield from update_ui(chatbot=chatbot, history=history)
    return
