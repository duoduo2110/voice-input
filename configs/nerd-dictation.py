import re

# nerd-dictation 用户配置：移除中文词间空格
# Vosk small-cn 会输出 "你 好 世 界" -> 期望 "你好世界"
# 保留中英/中数之间的一个空格，如 "你好 world 123"

def nerd_dictation_process(text):
    if not text:
        return text
    # 先把多空格压成一个
    text = re.sub(r'\s+', ' ', text).strip()
    # 反复删除 汉字-汉字 之间的空格，直到没有
    # \u4e00-\u9fff 覆盖常用汉字
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', text)
    # 删除汉字与中文标点之间的空格： "你好 ， 世界" -> "你好，世界"
    text = re.sub(r'\s+([，。？！、：；""''（）《》【】])', r'\1', text)
    text = re.sub(r'([，。？！、：；""''（）《》【】])\s+', r'\1', text)
    # 英文标点同理
    text = re.sub(r'\s+([,.!?;:])', r'\1', text)
    return text
